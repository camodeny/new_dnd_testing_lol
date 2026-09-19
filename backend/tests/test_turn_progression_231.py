"""Issue #231 — turn progression, action economy, end-turn, skip-player vote."""
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
from app.combat.service import (  # noqa: E402
    TURN_ENDED_EVENT,
    TURN_SKIPPED_EVENT,
    TURN_STARTED_EVENT,
    fulfill_human_initiative,
    get_active_encounter,
    encounter_view,
    roll_npc_initiative,
    start_encounter,
)
from app.combat.turns import (  # noqa: E402
    StaleTurnError,
    TurnAuthorizationError,
    TurnError,
    cast_skip_vote,
    consume_resource,
    end_turn,
    get_turn_state_row,
    grant_extra_resource,
    skip_tally,
    skip_threshold,
    turn_projection,
)
from app.dm.turns import coordinate_turn  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant, EncounterSkipVote, EncounterTurnState  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


def _engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _seed_world(db, *, third_member=False):
    owner = uuid.uuid4()
    player = uuid.uuid4()
    campaign_id = uuid.uuid4()
    owner_pc = uuid.uuid4()
    player_pc = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=player, email="player@example.com"),
        Campaign(id=campaign_id, owner_id=owner, name="Turn Table", revision=0),
        CampaignMember(campaign_id=campaign_id, user_id=owner, role="owner",
                       selected_character_id=owner_pc),
        CampaignMember(campaign_id=campaign_id, user_id=player, role="player",
                       selected_character_id=player_pc),
    ])
    if third_member:
        third = uuid.uuid4()
        db.add(Profile(id=third, email="third@example.com"))
        db.add(CampaignMember(campaign_id=campaign_id, user_id=third, role="player"))
    else:
        third = None
    db.flush()
    db.add_all([
        Character(id=owner_pc, owner_id=owner, name="Owner Blade", system="dnd5e"),
        Character(id=player_pc, owner_id=player, name="Player Bow", system="dnd5e"),
    ])
    db.add_all([
        Dnd5eCharacterSheet(character_id=owner_pc, owner_id=owner, character_name="Owner Blade",
                            dexterity=16, initiative_bonus=1, level=3),
        Dnd5eCharacterSheet(character_id=player_pc, owner_id=player, character_name="Player Bow",
                            dexterity=14, initiative_bonus=0, level=3),
    ])
    goblin = WorldEntity(campaign_id=campaign_id, entity_type="npc", name="Goblin Ambusher",
                         visibility="dm_only",
                         details={"initiative_modifier": 2, "dex_modifier": 1})
    db.add(goblin)
    db.flush()
    db.commit()
    thread = get_or_create_campaign_thread(db, campaign_id, created_by=owner)
    accept_submission(
        db, campaign_id=campaign_id, user_id=owner, character_id=owner_pc,
        raw_content="Goblins burst from the treeline!",
        segments=[{"type": "ic", "text": "Goblins burst from the treeline!"}],
        thread_id=str(thread.id),
    )
    db.commit()
    turn, attempt = coordinate_turn(db, campaign_id, str(thread.id))
    return {
        "campaign_id": campaign_id, "thread_id": str(thread.id),
        "owner": owner, "player": player, "third": third,
        "owner_pc": owner_pc, "player_pc": player_pc,
        "goblin_id": goblin.id, "turn_id": turn.id, "attempt_id": attempt.id,
    }


def _fixture(**kwargs):
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db, **kwargs)
    return fac, ctx


def _pc(db, encounter_id, character_id):
    return db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter_id,
            EncounterParticipant.character_id == character_id,
        )
    ).scalars().one()


def _revision(db, ctx):
    return int(db.get(Campaign, ctx["campaign_id"]).revision or 0)


def _ready_two_pc(db, ctx, *, owner_raw=10, player_raw=12, operation_id="op-ready-1"):
    """Owner total 14 (dex 3), player total 14 (dex 2): owner acts first."""
    encounter, _ = start_encounter(
        db, ctx["campaign_id"], operation_id=operation_id, expected_revision=0,
        actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
        source_attempt_id=ctx["attempt_id"],
        participants=[{"character_id": str(ctx["owner_pc"])},
                      {"character_id": str(ctx["player_pc"])}],
    )
    owner_p = _pc(db, encounter.id, ctx["owner_pc"])
    player_p = _pc(db, encounter.id, ctx["player_pc"])
    fulfill_human_initiative(
        db, encounter.id, owner_p.id, actor_id=ctx["owner"],
        payload={"source": "app", "raw_rolls": [owner_raw],
                 "modifier": owner_p.initiative_modifier,
                 "total": owner_raw + owner_p.initiative_modifier},
    )
    _, _, _, encounter, event = fulfill_human_initiative(
        db, encounter.id, player_p.id, actor_id=ctx["player"],
        payload={"source": "app", "raw_rolls": [player_raw],
                 "modifier": player_p.initiative_modifier,
                 "total": player_raw + player_p.initiative_modifier},
    )
    assert event is not None
    return db.get(Encounter, encounter.id)


# ── resource init / consumption / reset ─────────────────────────────────────


def test_resources_initialized_full_at_ready():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        assert encounter.status == "active"
        assert encounter.turn_sequence == 1
        assert encounter.turn_started_at is not None
        states = db.execute(
            select(EncounterTurnState).where(EncounterTurnState.encounter_id == encounter.id)
        ).scalars().all()
        assert len(states) == 2
        for state in states:
            assert state.action_available is True
            assert state.bonus_action_available is True
            assert state.reaction_available is True
            assert state.movement_remaining == state.movement_max == 30
        active_state = get_turn_state_row(db, encounter.id, encounter.active_participant_id)
        assert active_state.turn_started_at is not None
        projection = turn_projection(db, encounter)
        assert projection["turn_sequence"] == 1
        assert projection["round"] == 1
        assert projection["blocked"] is False
        assert set(projection["resources"]) == {
            str(s.participant_id) for s in states
        }


def test_consume_action_bonus_movement_and_double_consume_fails_closed():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        active_id = encounter.active_participant_id
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        assert active_id == owner_p.id
        state = consume_resource(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="action",
            expected_turn_sequence=1)
        assert state.action_available is False
        assert state.bonus_action_available is True
        with pytest.raises(TurnError, match="already consumed"):
            consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="action",
                             expected_turn_sequence=1)
        state = consume_resource(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="bonus_action",
            expected_turn_sequence=1)
        assert state.bonus_action_available is False
        state = consume_resource(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="movement", amount=10,
            expected_turn_sequence=1)
        assert state.movement_remaining == 20
        with pytest.raises(TurnError, match="insufficient movement"):
            consume_resource(
                db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="movement", amount=25,
                expected_turn_sequence=1)
        # Failed consumption leaves the budget untouched.
        assert db.get(EncounterTurnState, state.id).movement_remaining == 20


def test_reaction_consumable_off_turn_as_explicit_exception():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        assert encounter.active_participant_id != player_p.id
        state = consume_resource(
            db, encounter.id, player_p.id, actor_id=ctx["player"], resource="reaction",
            expected_turn_sequence=1)
        assert state.reaction_available is False
        with pytest.raises(TurnError, match="already consumed"):
            consume_resource(
                db, encounter.id, player_p.id, actor_id=ctx["player"], resource="reaction",
                expected_turn_sequence=1)


def test_out_of_turn_consume_rejected_and_counted():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        with pytest.raises(TurnError, match="only the active participant"):
            consume_resource(
                db, encounter.id, player_p.id, actor_id=ctx["player"], resource="action",
                expected_turn_sequence=1)
        assert db.get(Encounter, encounter.id).invalid_attempt_count == 1
        # Wrong actor for the active PC is a 403-class failure, also counted.
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        with pytest.raises(TurnAuthorizationError):
            consume_resource(
                db, encounter.id, owner_p.id, actor_id=ctx["player"], resource="action",
                expected_turn_sequence=1)
        assert db.get(Encounter, encounter.id).invalid_attempt_count == 2


def test_stale_consume_after_round_rollover_fails_closed():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        assert encounter.active_participant_id == owner_p.id
        # Cycle the owner back to active in round 2 without spending.
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=1, expected_revision=rev)
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["player"],
                 expected_turn_sequence=2, expected_revision=rev)
        revived = db.get(Encounter, encounter.id)
        assert revived.turn_sequence == 3
        assert revived.active_participant_id == owner_p.id
        assert revived.round == 2
        # A delayed command bound to turn 1 must not spend round 2's action.
        with pytest.raises(StaleTurnError):
            consume_resource(
                db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="action",
                expected_turn_sequence=1)
        fresh = get_turn_state_row(db, encounter.id, owner_p.id)
        assert fresh.action_available is True
        # The live turn still consumes exactly once.
        state = consume_resource(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="action",
            expected_turn_sequence=3)
        assert state.action_available is False


def test_extra_resources_seeded_consumed_and_reset():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        state = grant_extra_resource(
            db, encounter.id, owner_p.id, name="surge", maximum=1)
        assert state.extra_resources == {"surge": {"max": 1, "remaining": 1}}
        state = consume_resource(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="extra:surge",
            expected_turn_sequence=1)
        assert state.extra_resources == {"surge": {"max": 1, "remaining": 0}}
        with pytest.raises(TurnError, match="not available"):
            consume_resource(
                db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="extra:surge",
                expected_turn_sequence=1)
        # Full round later the extra resets to max at the owner's turn start.
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=1, expected_revision=rev)
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["player"],
                 expected_turn_sequence=2, expected_revision=rev)
        refreshed = get_turn_state_row(db, encounter.id, owner_p.id)
        assert refreshed.extra_resources == {"surge": {"max": 1, "remaining": 1}}


def test_resources_reset_at_own_turn_start():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="action",
                         expected_turn_sequence=1)
        consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"], resource="movement", amount=15,
                         expected_turn_sequence=1)
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=1, expected_revision=rev)
        spent = get_turn_state_row(db, encounter.id, owner_p.id)
        assert spent.action_available is False
        assert spent.movement_remaining == 15
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["player"],
                 expected_turn_sequence=2, expected_revision=rev)
        encounter = db.get(Encounter, encounter.id)
        assert encounter.round == 2
        assert encounter.active_participant_id == owner_p.id
        fresh = get_turn_state_row(db, encounter.id, owner_p.id)
        assert fresh.action_available is True
        assert fresh.bonus_action_available is True
        assert fresh.reaction_available is True
        assert fresh.movement_remaining == 30


# ── end turn / round rollover / duplicates ──────────────────────────────────


def test_normal_end_turn_advances_and_emits_events():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        rev = _revision(db, ctx)
        updated, ended_event, started_event = end_turn(
            db, encounter.id, actor_id=ctx["owner"],
            expected_turn_sequence=1, expected_revision=rev)
        assert updated.turn_sequence == 2
        assert updated.active_participant_id == player_p.id
        assert updated.active_index == 1
        assert updated.round == 1
        assert updated.last_turn_duration_ms is not None and updated.last_turn_duration_ms >= 0
        assert updated.last_end_turn_latency_ms is not None and updated.last_end_turn_latency_ms >= 0
        assert ended_event.event_type == TURN_ENDED_EVENT
        assert started_event.event_type == TURN_STARTED_EVENT
        assert ended_event.payload["ended_participant_id"] == str(owner_p.id)
        assert ended_event.payload["next_participant_id"] == str(player_p.id)
        assert ended_event.payload["skipped"] is False
        assert ended_event.payload["thread_id"] == encounter.thread_id
        types = [e.event_type for e in list_campaign_events(db, ctx["campaign_id"])]
        assert types[-2:] == [TURN_ENDED_EVENT, TURN_STARTED_EVENT]


def test_duplicate_end_turn_cannot_advance_twice():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=1, expected_revision=rev)
        assert db.get(Encounter, encounter.id).turn_sequence == 2
        # Lost acknowledgement retried with the stale source turn fails
        # closed instead of advancing again.
        with pytest.raises(StaleTurnError):
            end_turn(db, encounter.id, actor_id=ctx["owner"],
                     expected_turn_sequence=1, expected_revision=_revision(db, ctx))
        assert db.get(Encounter, encounter.id).turn_sequence == 2
        # The live sequence still advances exactly once.
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["player"],
                 expected_turn_sequence=2, expected_revision=rev)
        assert db.get(Encounter, encounter.id).turn_sequence == 3


def test_round_rollover_at_end_of_order():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=1, expected_revision=rev)
        rev = _revision(db, ctx)
        updated, ended_event, _ = end_turn(
            db, encounter.id, actor_id=ctx["player"],
            expected_turn_sequence=2, expected_revision=rev)
        assert updated.round == 2
        assert updated.active_participant_id == owner_p.id
        assert updated.active_index == 0
        assert ended_event.payload["rolled_over"] is True
        assert ended_event.payload["round"] == 2


def test_missing_player_blocks_foreign_end_turn():
    fac, ctx = _fixture()
    with fac() as db:
        # Player acts first: owner (or anyone else) cannot end their turn.
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        assert encounter.active_participant_id == player_p.id
        rev = _revision(db, ctx)
        with pytest.raises(TurnAuthorizationError):
            end_turn(db, encounter.id, actor_id=ctx["owner"],
                     expected_turn_sequence=1, expected_revision=rev)
        revived = db.get(Encounter, encounter.id)
        assert revived.turn_sequence == 1
        assert revived.active_participant_id == player_p.id
        assert revived.invalid_attempt_count == 1
        # The absent controller ends their own turn normally when back.
        rev = _revision(db, ctx)
        updated, _, _ = end_turn(
            db, encounter.id, actor_id=ctx["player"],
            expected_turn_sequence=1, expected_revision=rev)
        assert updated.turn_sequence == 2


def test_npc_turn_ended_by_owner_not_members():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-npc-first", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
            source_attempt_id=ctx["attempt_id"],
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"npc_entity_id": str(ctx["goblin_id"])}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        goblin = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.kind == "npc")
        ).scalars().one()
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [2],
                     "modifier": owner_p.initiative_modifier,
                     "total": 2 + owner_p.initiative_modifier})
        roll_npc_initiative(db, encounter.id, goblin.id, raw_d20=19)
        encounter = db.get(Encounter, encounter.id)
        assert encounter.active_participant_id == goblin.id
        with pytest.raises(TurnAuthorizationError):
            end_turn(db, encounter.id, actor_id=ctx["player"],
                     expected_turn_sequence=1, expected_revision=_revision(db, ctx))
        rev = _revision(db, ctx)
        updated, _, _ = end_turn(
            db, encounter.id, actor_id=ctx["owner"],
            expected_turn_sequence=1, expected_revision=rev)
        assert updated.active_participant_id == owner_p.id


# ── skip votes ──────────────────────────────────────────────────────────────


def test_skip_executes_on_owner_vote_and_generates_no_actions():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        assert encounter.active_participant_id == player_p.id
        rev = _revision(db, ctx)
        tally, executed, updated, skipped_event, started_event = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["owner"], expected_revision=rev,
            expected_turn_sequence=1)
        # Two-member table: one eligible voter (owner), threshold 1.
        assert tally == {
            "target_participant_id": str(player_p.id),
            "turn_sequence": 1,
            "votes": [str(ctx["owner"])],
            "vote_count": 1,
            "eligible_voter_count": 1,
            "threshold": 1,
            "reached": True,
        }
        assert executed is True
        assert updated.turn_sequence == 2
        assert updated.skipped_count == 1
        assert skipped_event.event_type == TURN_SKIPPED_EVENT
        assert started_event.event_type == TURN_STARTED_EVENT
        assert skipped_event.payload["skipped"] is True
        # The skipped PC was never AI-played: no budget consumed.
        skipped_state = get_turn_state_row(db, encounter.id, player_p.id)
        assert skipped_state.action_available is True
        assert skipped_state.bonus_action_available is True
        assert skipped_state.movement_remaining == 30
        types = [e.event_type for e in list_campaign_events(db, ctx["campaign_id"])]
        assert types[-2:] == [TURN_SKIPPED_EVENT, TURN_STARTED_EVENT]


def test_skip_threshold_partial_then_reached_with_three_members():
    fac, ctx = _fixture(third_member=True)
    with fac() as db:
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        assert skip_threshold(2) == 2
        rev = _revision(db, ctx)
        tally, executed, updated, skipped_event, _ = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["owner"], expected_revision=rev,
            expected_turn_sequence=1)
        assert executed is False
        assert skipped_event is None
        assert tally["vote_count"] == 1 and tally["threshold"] == 2
        assert updated.turn_sequence == 1  # still blocked on the player
        assert updated.blocked_since is not None
        blocked_ms = turn_projection(db, updated)
        assert blocked_ms["blocked"] is True
        # Duplicate vote by the same voter replays the tally, no double count.
        tally2, executed2, _, _, _ = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["owner"],
            expected_revision=_revision(db, ctx), expected_turn_sequence=1)
        assert executed2 is False
        assert tally2["vote_count"] == 1
        assert db.execute(
            select(EncounterSkipVote).where(
                EncounterSkipVote.encounter_id == encounter.id)
        ).scalars().all().__len__() == 1
        # Second distinct voter reaches the majority: skip executes.
        rev = _revision(db, ctx)
        tally3, executed3, updated3, skipped_event3, _ = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["third"], expected_revision=rev,
            expected_turn_sequence=1)
        assert executed3 is True
        assert tally3["vote_count"] == 2
        assert skipped_event3 is not None
        assert updated3.turn_sequence == 2
        assert updated3.blocked_since is None


def test_stale_skip_vote_after_round_rollover_fails_closed():
    fac, ctx = _fixture(third_member=True)
    with fac() as db:
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        assert encounter.active_participant_id == player_p.id
        # Cycle the same PC back to active in round 2 via normal end-turns.
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["player"],
                 expected_turn_sequence=1, expected_revision=rev)
        rev = _revision(db, ctx)
        end_turn(db, encounter.id, actor_id=ctx["owner"],
                 expected_turn_sequence=2, expected_revision=rev)
        revived = db.get(Encounter, encounter.id)
        assert revived.turn_sequence == 3
        assert revived.active_participant_id == player_p.id
        # A delayed vote bound to turn 1 must not count against round 2.
        with pytest.raises(StaleTurnError):
            cast_skip_vote(db, encounter.id, player_p.id, voter_id=ctx["owner"],
                           expected_revision=_revision(db, ctx),
                           expected_turn_sequence=1)
        assert db.execute(
            select(EncounterSkipVote).where(
                EncounterSkipVote.encounter_id == encounter.id,
                EncounterSkipVote.turn_sequence == 3)
        ).scalars().first() is None
        # The live turn still accepts a fresh vote (partial tally, threshold 2).
        tally, executed, _, _, _ = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["owner"],
            expected_revision=_revision(db, ctx), expected_turn_sequence=3)
        assert executed is False
        assert tally["vote_count"] == 1


def test_private_thread_nonreaders_excluded_from_skip_threshold():
    from app.runtime.threads import create_private_thread

    fac, ctx = _fixture(third_member=True)
    with fac() as db:
        thread = create_private_thread(
            db, campaign_id=ctx["campaign_id"], created_by=ctx["owner"],
            member_ids=[ctx["owner"], ctx["player"]], title="Side Room",
        )
        db.commit()
        accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["owner"],
            character_id=ctx["owner_pc"],
            raw_content="Something moves in the dark!",
            segments=[{"type": "ic", "text": "Something moves in the dark!"}],
            thread_id=str(thread.id),
        )
        db.commit()
        turn, attempt = coordinate_turn(db, ctx["campaign_id"], str(thread.id))
        db.commit()
        private_turn_id, private_attempt_id = turn.id, attempt.id
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-private-skip-231",
            expected_revision=_revision(db, ctx), actor_id=ctx["owner"],
            source_turn_id=private_turn_id, source_attempt_id=private_attempt_id,
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"character_id": str(ctx["player_pc"])}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [5],
                     "modifier": owner_p.initiative_modifier,
                     "total": 5 + owner_p.initiative_modifier})
        _, _, _, encounter, _ = fulfill_human_initiative(
            db, encounter.id, player_p.id, actor_id=ctx["player"],
            payload={"source": "app", "raw_rolls": [18],
                     "modifier": player_p.initiative_modifier,
                     "total": 18 + player_p.initiative_modifier})
        assert encounter.active_participant_id == player_p.id
        # The third campaign member cannot read the private encounter thread,
        # so they are not an eligible skip voter and do not inflate the bar.
        tally = skip_tally(db, encounter, player_p.id)
        assert tally["eligible_voter_count"] == 1
        assert tally["threshold"] == 1
        with pytest.raises(TurnAuthorizationError):
            cast_skip_vote(db, encounter.id, player_p.id, voter_id=ctx["third"],
                           expected_revision=_revision(db, ctx),
                           expected_turn_sequence=1)
        tally2, executed, updated, skipped_event, _ = cast_skip_vote(
            db, encounter.id, player_p.id, voter_id=ctx["owner"],
            expected_revision=_revision(db, ctx), expected_turn_sequence=1)
        assert executed is True
        assert skipped_event is not None
        assert updated.turn_sequence == 2
        assert tally2["eligible_voter_count"] == 1


def test_skip_authorization_and_target_rules():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        outsider = uuid.uuid4()
        db.add(Profile(id=outsider, email="outsider@example.com"))
        db.commit()
        rev = _revision(db, ctx)
        with pytest.raises(TurnAuthorizationError):
            cast_skip_vote(db, encounter.id, player_p.id, voter_id=outsider,
                           expected_revision=rev, expected_turn_sequence=1)
        # Votes target the blocking active participant only.
        with pytest.raises(TurnError, match="currently active"):
            cast_skip_vote(db, encounter.id, owner_p.id, voter_id=ctx["player"],
                           expected_revision=rev, expected_turn_sequence=1)
        # The controller ends their own turn; they cannot self-skip-vote.
        with pytest.raises(TurnError, match="own turn"):
            cast_skip_vote(db, encounter.id, player_p.id, voter_id=ctx["player"],
                           expected_revision=rev, expected_turn_sequence=1)
        assert db.get(Encounter, encounter.id).turn_sequence == 1


def test_skip_rejected_for_npc_targets():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-skip-npc", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
            source_attempt_id=ctx["attempt_id"],
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"npc_entity_id": str(ctx["goblin_id"])}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        goblin = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.kind == "npc")
        ).scalars().one()
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [2],
                     "modifier": owner_p.initiative_modifier,
                     "total": 2 + owner_p.initiative_modifier})
        roll_npc_initiative(db, encounter.id, goblin.id, raw_d20=19)
        with pytest.raises(TurnError, match="NPC/monster"):
            cast_skip_vote(db, encounter.id, goblin.id, voter_id=ctx["owner"],
                           expected_revision=_revision(db, ctx), expected_turn_sequence=1)


# ── reconnect / chat / realtime ─────────────────────────────────────────────


def test_state_reconstructs_exactly_after_reconnect(tmp_path):
    path = tmp_path / "turns.db"
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db, third_member=True)
        encounter = _ready_two_pc(
            db, ctx, owner_raw=5, player_raw=18, operation_id="op-reconnect")
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        consume_resource(db, encounter.id, player_p.id, actor_id=ctx["player"],
                         resource="movement", amount=5, expected_turn_sequence=1)
        cast_skip_vote(db, encounter.id, player_p.id, voter_id=ctx["owner"],
                       expected_revision=_revision(db, ctx), expected_turn_sequence=1)
        cast_skip_vote(db, encounter.id, player_p.id, voter_id=ctx["third"],
                       expected_revision=_revision(db, ctx), expected_turn_sequence=1)
        encounter_id = encounter.id
        assert db.get(Encounter, encounter_id).turn_sequence == 2
    eng.dispose()

    eng2 = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    fac2 = sessionmaker(bind=eng2, expire_on_commit=False)
    with fac2() as db:
        revived = get_active_encounter(db, ctx["campaign_id"])
        assert revived is not None and revived.id == encounter_id
        assert revived.turn_sequence == 2
        assert revived.skipped_count == 1
        assert revived.round == 1
        projection = turn_projection(db, revived)
        assert projection["turn_sequence"] == 2
        assert projection["skipped_count"] == 1
        owner_state = get_turn_state_row(db, encounter_id, owner_p.id)
        assert owner_state.movement_remaining == 30
        player_state = get_turn_state_row(db, encounter_id, player_p.id)
        assert player_state.movement_remaining == 25  # spent movement survived
        assert player_state.action_available is True
        # Votes were sequence-scoped: the old tally does not leak forward.
        assert db.execute(
            select(EncounterSkipVote).where(
                EncounterSkipVote.encounter_id == encounter_id,
                EncounterSkipVote.turn_sequence == 2)
        ).scalars().first() is None
        # Mechanical progression resumes from the restored state.
        rev = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        updated, _, _ = end_turn(
            db, encounter_id, actor_id=ctx["owner"],
            expected_turn_sequence=2, expected_revision=rev)
        assert updated.turn_sequence == 3
        assert updated.round == 2


def test_ic_ooc_chat_non_blocking_during_active_turn():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx, owner_raw=5, player_raw=18)
        assert encounter.status == "active"
        # Another actor's mechanical turn never gates table conversation.
        submission = accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["owner"],
            character_id=ctx["owner_pc"],
            raw_content="<ic>I ready my blade!</ic><ooc>holding action btw</ooc>",
            segments=[{"type": "ic", "text": "I ready my blade!"},
                      {"type": "ooc", "text": "holding action btw"}],
            thread_id=ctx["thread_id"],
        )
        db.commit()
        assert submission.resolution_status == "accepted"
        assert db.get(Encounter, encounter.id).turn_sequence == 1


def test_turn_realtime_projections_have_stable_ids_and_no_hidden_stats():
    from app.realtime.service import (
        InMemoryRealtimePublisher,
        build_encounter_turn_event,
        get_realtime_publisher,
        publish_encounter_turn,
        set_realtime_publisher,
    )

    fac, ctx = _fixture()
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        with fac() as db:
            encounter = _ready_two_pc(db, ctx)
            assert publish_encounter_turn(db, encounter, "started") is True
            rev = _revision(db, ctx)
            end_turn(db, encounter.id, actor_id=ctx["owner"],
                     expected_turn_sequence=1, expected_revision=rev)
            encounter = db.get(Encounter, encounter.id)
            assert publish_encounter_turn(db, encounter, "ended") is True
            assert publish_encounter_turn(db, encounter, "started") is True
            kinds = [p["payload"]["type"] for p in recorder.published
                     if str(p["payload"].get("type", "")).startswith("encounter.turn_")]
            assert kinds == [
                "encounter.turn_started", "encounter.turn_ended", "encounter.turn_started"]
            # Stable ids keyed by sequence+kind; no budget/stat breakdowns.
            turn_payloads = [p["payload"] for p in recorder.published
                             if str(p["payload"].get("type", "")).startswith("encounter.turn_")]
            first, second, third = turn_payloads
            assert first["event_id"] == f"encounter:{encounter.id}:turn:1:started"
            assert second["event_id"] == f"encounter:{encounter.id}:turn:2:ended"
            assert third["event_id"] == f"encounter:{encounter.id}:turn:2:started"
            assert "initiative_modifier" not in str(recorder.published)
            assert "action_available" not in str(recorder.published)
            assert build_encounter_turn_event(encounter, "skipped")["type"] == "encounter.turn_skipped"
            assert publish_encounter_turn(db, encounter, "bogus") is False
    finally:
        set_realtime_publisher(previous)


def test_encounter_view_carries_turn_block_for_snapshot():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_two_pc(db, ctx)
        view = encounter_view(db, encounter, ctx["owner"], is_owner=True)
        assert view["turn"] is not None
        assert view["turn"]["turn_sequence"] == 1
        assert view["turn"]["active_participant_id"] == str(encounter.active_participant_id)
        assert view["turn_sequence"] == 1
        assert view["skipped_count"] == 0


def test_hidden_npc_speed_redacted_for_non_owners():
    fac, ctx = _fixture()
    with fac() as db:
        swift = WorldEntity(
            campaign_id=ctx["campaign_id"], entity_type="npc", name="Swift Stalker",
            visibility="dm_only",
            details={"initiative_modifier": 1, "dex_modifier": 0, "speed": 50},
        )
        db.add(swift)
        db.flush()
        db.commit()
        swift_id = swift.id
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-hidden-speed", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
            source_attempt_id=ctx["attempt_id"],
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"npc_entity_id": str(swift_id)}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.kind == "npc")
        ).scalars().one()
        assert npc.stat_visibility == "dm_private"
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [10],
                     "modifier": owner_p.initiative_modifier,
                     "total": 10 + owner_p.initiative_modifier})
        roll_npc_initiative(db, encounter.id, npc.id, raw_d20=10)
        encounter = db.get(Encounter, encounter.id)
        assert encounter.status == "active"
        npc_id = str(npc.id)
        owner_id, player_id = ctx["owner"], ctx["player"]
        # Owner (DM runtime path) sees the canonical derived speed.
        owner_proj = turn_projection(db, encounter, viewer_id=owner_id, is_owner=True)
        assert owner_proj["resources"][npc_id]["movement_max"] == 50
        assert owner_proj["resources"][npc_id]["movement_remaining"] == 50
        # A thread reader who is not the owner cannot reconstruct it.
        player_proj = turn_projection(db, encounter, viewer_id=player_id, is_owner=False)
        assert player_proj["resources"][npc_id]["movement_max"] is None
        assert player_proj["resources"][npc_id]["movement_remaining"] is None
        assert player_proj["resources"][npc_id]["extra_resources"] == {}
        assert "50" not in str(player_proj["resources"][npc_id])
        # The PC's own budget stays visible to the non-owner.
        assert player_proj["resources"][str(owner_p.id)]["movement_max"] == 30
        # Same redaction rides the snapshot view and the default (fail-closed) read.
        owner_view = encounter_view(db, encounter, owner_id, is_owner=True)
        player_view = encounter_view(db, encounter, player_id, is_owner=False)
        assert owner_view["turn"]["resources"][npc_id]["movement_max"] == 50
        assert player_view["turn"]["resources"][npc_id]["movement_max"] is None
        default_proj = turn_projection(db, encounter)
        assert default_proj["resources"][npc_id]["movement_max"] is None


# ── HTTP transport ──────────────────────────────────────────────────────────


def test_http_end_turn_replay_stale_and_skip_vote(monkeypatch):
    from fastapi.testclient import TestClient

    from app.realtime.service import (
        InMemoryRealtimePublisher,
        get_realtime_publisher,
        set_realtime_publisher,
    )
    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    fac, ctx = _fixture()
    campaign_id = str(ctx["campaign_id"])
    owner_id, player_id = str(ctx["owner"]), str(ctx["player"])

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, uuid.UUID(request.headers["x-test-user"]))

    monkeypatch.setattr("app.combat.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        client = TestClient(app)
        owner_h = {"x-test-user": owner_id}
        player_h = {"x-test-user": player_id}
        with fac() as db:
            encounter = _ready_two_pc(
                db, ctx, owner_raw=5, player_raw=18, operation_id="op-http-231")
            encounter_id = str(encounter.id)
            player_p = _pc(db, encounter.id, ctx["player_pc"])
            rev = _revision(db, ctx)
        # Wrong actor cannot end the player's turn.
        foreign = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-turn",
            json={"expected_revision": rev, "expected_turn_sequence": 1},
            headers={**owner_h, "Idempotency-Key": "end-foreign-1"},
        )
        assert foreign.status_code == 403, foreign.text
        # Controller ends their turn; replay with the same key is identical.
        body = {"expected_revision": rev, "expected_turn_sequence": 1}
        first = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-turn",
            json=body, headers={**player_h, "Idempotency-Key": "end-player-1"})
        assert first.status_code == 200, first.text
        assert first.headers["X-Idempotent-Replay"] == "false"
        assert first.json()["turn_sequence"] == 2
        replay = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-turn",
            json=body, headers={**player_h, "Idempotency-Key": "end-player-1"})
        assert replay.status_code == 200
        assert replay.headers["X-Idempotent-Replay"] == "true"
        assert replay.json()["turn_sequence"] == 2
        # Same stale source turn with a fresh key fails closed, never advances.
        stale = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-turn",
            json=body, headers={**player_h, "Idempotency-Key": "end-player-2"})
        assert stale.status_code == 409, stale.text
        assert stale.headers["X-Current-Turn-Sequence"] == "2"
        # Turn-state endpoint reflects the restored mechanical state.
        state = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-state",
            headers=owner_h)
        assert state.status_code == 200
        assert state.json()["turn"]["turn_sequence"] == 2
        # Out-of-turn consumption is rejected over HTTP.
        oot = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-resources/consume",
            json={"participant_id": str(player_p.id), "resource": "action",
                  "expected_turn_sequence": 2},
            headers={**player_h, "Idempotency-Key": "consume-oot-1"})
        assert oot.status_code == 422, oot.text
        # Missing turn binding is a 400; stale turn binding is a 409.
        missing_seq = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-resources/consume",
            json={"participant_id": str(player_p.id), "resource": "action"},
            headers={**player_h, "Idempotency-Key": "consume-missing-seq-1"})
        assert missing_seq.status_code == 400, missing_seq.text
        stale_consume = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-resources/consume",
            json={"participant_id": str(player_p.id), "resource": "action",
                  "expected_turn_sequence": 1},
            headers={**player_h, "Idempotency-Key": "consume-stale-1"})
        assert stale_consume.status_code == 409, stale_consume.text
        assert stale_consume.headers["X-Current-Turn-Sequence"] == "2"
        # Owner skip-votes the now-active owner PC? No — owner is active;
        # advance to the player then skip them via owner vote.
        first_active = first.json()["encounter"]["active_participant_id"]
        assert first_active is not None
        with fac() as db:
            rev3 = _revision(db, ctx)
            active_now = db.get(Encounter, uuid.UUID(encounter_id)).active_participant_id
        assert str(active_now) == first_active
        skip = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/skip-votes",
            json={"expected_revision": rev3, "target_participant_id": first_active,
                  "expected_turn_sequence": 2},
            headers={**player_h, "Idempotency-Key": "skip-1"})
        # Player votes to skip the active owner: eligible voters excluding the
        # owner are {player} → threshold 1 → executes immediately.
        assert skip.status_code == 200, skip.text
        assert skip.json()["executed"] is True
        assert skip.json()["tally"]["vote_count"] == 1
        # Missing turn binding is a 400; stale turn binding is a 409.
        missing_skip = client.post(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/skip-votes",
            json={"expected_revision": rev3,
                  "target_participant_id": skip.json()["encounter"]["active_participant_id"]},
            headers={**player_h, "Idempotency-Key": "skip-missing-seq-1"})
        assert missing_skip.status_code == 400, missing_skip.text
        # Realtime projections fired for ended + started + skipped turns.
        published_types = [p["payload"]["type"] for p in recorder.published]
        assert "encounter.turn_ended" in published_types
        assert "encounter.turn_started" in published_types
        assert "encounter.turn_skipped" in published_types
    finally:
        set_realtime_publisher(previous)
        app.dependency_overrides.clear()
