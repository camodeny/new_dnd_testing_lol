"""Issue #239 — DM-controlled encounter end and post-combat consequence hooks.

The AI is the only DM: encounters end only via the ``end_encounter`` staged
effect (``end_encounter_inline``). The ``encounter.ended`` domain event is
staged by the turn commit, so tests asserting on the event drive the end
through ``commit_turn``; row-level behavior goes through
``tests.support.combat.dm_end_encounter``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

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
from app.campaigns.replacements import get_lifecycle  # noqa: E402
from app.combat.ending import (  # noqa: E402
    END_OUTCOMES,
    EndEncounterError,
    build_final_snapshot,
    end_encounter_inline,
    list_end_followups,
)
from app.combat.maps import MapError  # noqa: E402
from app.combat.service import (  # noqa: E402
    ENCOUNTER_ENDED_EVENT,
    THREAD_SCOPED_EVENT_TYPES,
    encounter_view,
    fulfill_human_initiative,
    get_active_encounter,
)
from app.combat.turns import TurnError, cast_skip_vote, consume_resource, end_turn  # noqa: E402
from app.dm.contract import normalize_contract  # noqa: E402
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started, stage_validated_attempt  # noqa: E402
from app.post_turn.service import is_post_turn_relevant  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant, EncounterTurnState  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt, DMStream, DMStreamChunk  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402
from tests.support.combat import dm_end_encounter, dm_start_encounter  # noqa: E402


def _engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _seed_world(db):
    owner = uuid.uuid4()
    player = uuid.uuid4()
    campaign_id = uuid.uuid4()
    owner_pc = uuid.uuid4()
    player_pc = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=player, email="player@example.com"),
        Campaign(id=campaign_id, owner_id=owner, name="End Table", revision=0),
        CampaignMember(campaign_id=campaign_id, user_id=owner, role="owner",
                       selected_character_id=owner_pc),
        CampaignMember(campaign_id=campaign_id, user_id=player, role="player",
                       selected_character_id=player_pc),
    ])
    db.flush()
    db.add_all([
        Character(id=owner_pc, owner_id=owner, name="Owner Blade", system="dnd5e"),
        Character(id=player_pc, owner_id=player, name="Player Bow", system="dnd5e"),
    ])
    db.add_all([
        Dnd5eCharacterSheet(character_id=owner_pc, owner_id=owner, character_name="Owner Blade",
                            dexterity=16, initiative_bonus=1, level=3,
                            hit_points_current=24, hit_points_max=28, hit_points_temp=0),
        Dnd5eCharacterSheet(character_id=player_pc, owner_id=player, character_name="Player Bow",
                            dexterity=14, initiative_bonus=0, level=3,
                            hit_points_current=18, hit_points_max=22, hit_points_temp=0),
    ])
    goblin = WorldEntity(campaign_id=campaign_id, entity_type="npc", name="Goblin Ambusher",
                         visibility="dm_only",
                         details={"initiative_modifier": 2, "dex_modifier": 1,
                                  "hit_points": {"current": 7, "maximum": 7, "temporary": 0}})
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
        "owner": owner, "player": player,
        "owner_pc": owner_pc, "player_pc": player_pc,
        "goblin_id": goblin.id, "turn_id": turn.id, "attempt_id": attempt.id,
    }


def _fixture():
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db)
    return fac, ctx


def _pc(db, encounter_id, character_id):
    return db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter_id,
            EncounterParticipant.character_id == character_id,
        )
    ).scalars().one()


def _npc(db, encounter_id, goblin_id):
    return db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter_id,
            EncounterParticipant.npc_entity_id == goblin_id,
        )
    ).scalars().one()


def _revision(db, ctx):
    return int(db.get(Campaign, ctx["campaign_id"]).revision or 0)


def _start_party(db, ctx, *, with_npc=False, effect_id=None, map=None):
    parts = [{"character_id": str(ctx["owner_pc"])},
             {"character_id": str(ctx["player_pc"])}]
    if with_npc:
        parts.append({"npc_entity_id": str(ctx["goblin_id"])})
    return dm_start_encounter(
        db, ctx["campaign_id"], ctx["turn_id"], ctx["attempt_id"], parts,
        map=map, npc_d20=7 if with_npc else None, effect_id=effect_id,
    )


def _ready_party(db, ctx, *, with_npc=False, effect_id=None, map=None):
    encounter = _start_party(db, ctx, with_npc=with_npc, effect_id=effect_id, map=map)
    owner_p = _pc(db, encounter.id, ctx["owner_pc"])
    player_p = _pc(db, encounter.id, ctx["player_pc"])
    fulfill_human_initiative(
        db, encounter.id, owner_p.id, actor_id=ctx["owner"],
        payload={"source": "app", "raw_rolls": [10],
                 "modifier": owner_p.initiative_modifier,
                 "total": 10 + owner_p.initiative_modifier},
    )
    fulfill_human_initiative(
        db, encounter.id, player_p.id, actor_id=ctx["player"],
        payload={"source": "app", "raw_rolls": [12],
                 "modifier": player_p.initiative_modifier,
                 "total": 12 + player_p.initiative_modifier},
    )
    return db.get(Encounter, encounter.id)


def _end(db, ctx, encounter, *, outcome="victory", reason="The goblins are slain.",
         participant_outcomes=None, effect_id=None):
    return dm_end_encounter(
        db, ctx["campaign_id"], ctx["turn_id"], ctx["attempt_id"], encounter.id,
        outcome=outcome, reason=reason, participant_outcomes=participant_outcomes,
        effect_id=effect_id,
    )


def _commit_turn_with_effects(db, ctx, staged_effects):
    """Run the seeded turn's commit ceremony with the given staged effects."""
    contract = normalize_contract({
        "contract_version": "dm_turn_contract_v1",
        "mode": "respond",
        "reason": "the fight ends",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{"text": "The goblins flee.", "claim_kind": "observation",
                        "origin": "dm_adjudication"}],
        }],
        "staged_effects": staged_effects,
    })
    turn = db.get(DmTurn, ctx["turn_id"])
    attempt = db.get(DmTurnAttempt, ctx["attempt_id"])
    stage_validated_attempt(db, attempt.id, contract)
    stream = DMStream(
        id=uuid.uuid4(), campaign_id=turn.campaign_id,
        thread_id=uuid.UUID(str(turn.thread_id)),
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        status="streaming", audience=turn.audience,
    )
    db.add(stream)
    db.flush()
    db.add(DMStreamChunk(id=uuid.uuid4(), stream_id=stream.id, sequence=0,
                         text="The goblins flee.", byte_length=17))
    stream.first_chunk_at = datetime.now(timezone.utc)
    stream.chunk_count = 1
    db.flush()
    mark_streaming_started(db, turn.id, attempt.id, stream.id)
    commit_turn(db, turn.id, attempt.id, expected_revision=_revision(db, ctx))


def _commit_end(db, ctx, encounter, *, outcome="victory", reason="The fight is over.",
                participant_outcomes=None, effect_id="fx-end-commit"):
    """End via the DM staged effect inside a real turn commit (stages the event)."""
    arguments = {"encounter_id": str(encounter.id), "outcome": outcome, "reason": reason}
    if participant_outcomes is not None:
        arguments["participant_outcomes"] = participant_outcomes
    _commit_turn_with_effects(db, ctx, [{
        "id": effect_id, "effect_type": "end_encounter", "arguments": arguments,
    }])
    return db.get(Encounter, encounter.id)


def _ended_event(db, ctx, encounter_id):
    return next(
        e for e in list_campaign_events(db, ctx["campaign_id"])
        if e.event_type == ENCOUNTER_ENDED_EVENT
        and (e.payload or {}).get("encounter_id") == str(encounter_id)
    )


# ── supported fictional reasons (not only all-enemies-dead) ─────────────────


@pytest.mark.parametrize("outcome", sorted(END_OUTCOMES))
def test_end_supported_outcomes(outcome):
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        updated = _end(
            db, ctx, encounter, outcome=outcome,
            reason=f"Combat resolves: {outcome}.",
            effect_id=f"fx-end-{outcome}",
        )
        assert updated.status == "ended"
        assert updated.end_outcome == outcome
        # The inline end stages rows only; the turn commit stages the event.
        assert updated.ended_event_id is None
        hooks = list_end_followups(db, encounter.id)
        assert {h.hook_type for h in hooks} == {
            "loot_availability", "xp_progression", "death_aftermath",
            "custody_state", "post_turn_consolidation",
        }
        assert all(h.status == "pending" for h in hooks)


def test_kill_based_end_with_fleeing_enemy():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        updated = _end(
            db, ctx, encounter, outcome="victory",
            reason="Two goblins slain; the last flees into the treeline.",
            participant_outcomes={str(npc.id): "fled"},
        )
        assert updated.status == "ended"
        assert updated.end_participant_outcomes[str(npc.id)] == "fled"


def test_surrender_end_with_custody_outcomes():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        updated = _end(
            db, ctx, encounter, outcome="surrender",
            reason="The goblin drops its blade and yields.",
            participant_outcomes={str(npc.id): "surrendered"},
        )
        assert updated.end_outcome == "surrender"
        assert updated.end_participant_outcomes[str(npc.id)] == "surrendered"
        hooks = {h.hook_type: h for h in list_end_followups(db, encounter.id)}
        assert hooks["custody_state"].status == "pending"


def test_party_retreat_end():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        updated = _end(
            db, ctx, encounter, outcome="retreat",
            reason="The party breaks off and falls back to the road.",
            participant_outcomes={
                str(owner_p.id): "retreated", str(player_p.id): "retreated",
            },
        )
        assert updated.end_outcome == "retreat"
        assert updated.end_participant_outcomes[str(owner_p.id)] == "retreated"


def test_capture_end():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        updated = _end(
            db, ctx, encounter, outcome="capture",
            reason="The archers net the scout and drag them off.",
            participant_outcomes={str(player_p.id): "captured"},
        )
        assert updated.end_outcome == "capture"
        assert updated.end_participant_outcomes[str(player_p.id)] == "captured"


def test_character_death_persists_into_post_combat():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        updated = _end(
            db, ctx, encounter, outcome="defeat",
            reason="The owlbear crushes the scout.",
            participant_outcomes={str(player_p.id): "slain"},
        )
        assert updated.status == "ended"
        lifecycle = get_lifecycle(db, ctx["campaign_id"], ctx["player_pc"])
        assert lifecycle is not None and lifecycle.status == "dead"
        # Death aftermath hook is seeded pending for downstream processing.
        hooks = {h.hook_type: h for h in list_end_followups(db, encounter.id)}
        assert hooks["death_aftermath"].status == "pending"


def test_end_validates_outcome_reason_and_participants():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        campaign = db.get(Campaign, ctx["campaign_id"])
        with pytest.raises(EndEncounterError, match="outcome must be"):
            end_encounter_inline(
                db, campaign, db.get(Encounter, encounter.id),
                {"encounter_id": str(encounter.id), "outcome": "everyone-wins",
                 "reason": "Nope."},
                "fx-bad-1",
            )
        with pytest.raises(EndEncounterError, match="reason is required"):
            end_encounter_inline(
                db, campaign, db.get(Encounter, encounter.id),
                {"encounter_id": str(encounter.id), "outcome": "victory",
                 "reason": "  "},
                "fx-bad-2",
            )
        with pytest.raises(EndEncounterError, match="unknown participants"):
            end_encounter_inline(
                db, campaign, db.get(Encounter, encounter.id),
                {"encounter_id": str(encounter.id), "outcome": "victory",
                 "reason": "Done.",
                 "participant_outcomes": {str(uuid.uuid4()): "fled"}},
                "fx-bad-3",
            )
        # Failed end transactions leave the encounter active, never half-closed.
        assert db.get(Encounter, encounter.id).status == "active"
        assert list_end_followups(db, encounter.id) == []


# ── frozen progression + preserved final state ──────────────────────────────


def test_ending_closes_turn_reaction_progression():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        seq = int(encounter.turn_sequence or 0)
        _end(db, ctx, encounter)
        ended = db.get(Encounter, encounter.id)
        assert ended.status == "ended"
        assert get_active_encounter(db, ctx["campaign_id"]) is None
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        with pytest.raises(TurnError, match="has ended"):
            end_turn(db, encounter.id, actor_id=ctx["owner"],
                     expected_turn_sequence=seq, expected_revision=_revision(db, ctx))
        with pytest.raises(TurnError, match="has ended"):
            consume_resource(db, encounter.id, player_p.id, actor_id=ctx["player"],
                             resource="reaction", expected_turn_sequence=seq)
        with pytest.raises(TurnError, match="has ended"):
            consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                             resource="action", expected_turn_sequence=seq)
        with pytest.raises(TurnError):
            cast_skip_vote(db, encounter.id, owner_p.id, voter_id=ctx["player"],
                           expected_revision=_revision(db, ctx),
                           expected_turn_sequence=seq)


def test_ending_blocks_movement_after_end():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(
            db, ctx, with_npc=True, map={"width": 8, "height": 8})
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        _end(db, ctx, encounter)
        from app.combat.maps import move_participant

        with pytest.raises(MapError, match="active encounter"):
            move_participant(db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                             to_col=2, to_row=2,
                             expected_turn_sequence=int(encounter.turn_sequence or 0),
                             expected_revision=_revision(db, ctx),
                             operation_id="op-move-after-end")


def test_final_state_preserved_for_history_and_scene_play():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        # Spend the active PC's action so the frozen budget is observable.
        consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                         resource="action",
                         expected_turn_sequence=int(encounter.turn_sequence or 0))
        _end(db, ctx, encounter)
        final = build_final_snapshot(db, db.get(Encounter, encounter.id))
        by_name = {p["display_name"]: p for p in final["participants"]}
        assert by_name["Owner Blade"]["hit_points"] == {
            "current": 24, "maximum": 28, "temporary": 0}
        assert by_name["Player Bow"]["hit_points"]["current"] == 18
        assert by_name["Owner Blade"]["turn_budget"]["action_available"] is False
        # Turn budgets and canonical HP rows still exist for scene play.
        states = db.execute(
            select(EncounterTurnState).where(EncounterTurnState.encounter_id == encounter.id)
        ).scalars().all()
        assert len(states) == 2
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == ctx["owner_pc"])
        ).scalars().first()
        assert int(sheet.hit_points_current) == 24
        # builder agrees with the committed rows.
        assert final["participant_count"] == 2


def test_ended_event_feeds_history_and_post_turn():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        _commit_end(db, ctx, encounter)
        events = list_campaign_events(db, ctx["campaign_id"])
        kinds = [e.event_type for e in events]
        assert ENCOUNTER_ENDED_EVENT in kinds
        ended_row = _ended_event(db, ctx, encounter.id)
        assert ended_row.sequence == int(db.get(Campaign, ctx["campaign_id"]).revision)
        assert ended_row.payload["outcome"] == "victory"
        assert "final_state" in ended_row.payload
        # The ended event is post-turn relevant: nothing accepted is dropped.
        assert is_post_turn_relevant(ENCOUNTER_ENDED_EVENT) is True
        # Thread-scoped like the other lifecycle events.
        assert ENCOUNTER_ENDED_EVENT in THREAD_SCOPED_EVENT_TYPES
        assert ended_row.payload["thread_id"] == ctx["thread_id"]


def test_hidden_enemy_final_data_stays_scoped():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        _end(
            db, ctx, encounter,
            participant_outcomes={str(npc.id): "slain"},
        )
        # Player projection never carries hidden NPC stat breakdowns.
        player_view = encounter_view(
            db, db.get(Encounter, encounter.id), ctx["player"])
        goblin_view = next(p for p in player_view["participants"]
                           if p.get("npc_entity_id") == str(ctx["goblin_id"]))
        assert "initiative_modifier" not in goblin_view
        assert "raw_roll" not in goblin_view
        # Final snapshot flags the redaction instead of leaking HP.
        final = build_final_snapshot(db, db.get(Encounter, encounter.id))
        final_npc = next(p for p in final["participants"]
                         if p["id"] == str(npc.id))
        assert final_npc["hit_points"] is None
        assert final_npc["detail_redacted"] is True
        # Turn projection is closed; map/final positions still readable.
        assert player_view["turn"] is None


def test_end_projection_redacts_reason_and_hidden_fates_for_every_player():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        assert npc.stat_visibility == "dm_private"
        owner_pc = _pc(db, encounter.id, ctx["owner_pc"])
        secret_reason = "The hidden ambusher slips away with the stolen seal."
        _end(
            db, ctx, encounter, outcome="escape", reason=secret_reason,
            participant_outcomes={str(npc.id): "fled", str(owner_pc.id): "standing"},
        )
        ended = db.get(Encounter, encounter.id)
        # The owner is a player too: same redaction as any member.
        for viewer in (ctx["owner"], ctx["player"]):
            player_view = encounter_view(db, ended, viewer)
            assert player_view["end_outcome"] == "escape"
            assert player_view["end_reason"] is None
            assert secret_reason not in str(player_view)
            assert str(npc.id) not in (player_view["end_participant_outcomes"] or {})
            assert player_view["end_participant_outcomes"][str(owner_pc.id)] == "standing"


def test_ended_event_hidden_from_member_history():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        secret_reason = "The hidden ambusher slips away with the stolen seal."
        _commit_end(
            db, ctx, encounter, outcome="escape", reason=secret_reason,
            participant_outcomes={str(npc.id): "fled"},
            effect_id="fx-end-evh",
        )
        event = _ended_event(db, ctx, encounter.id)
        assert event.visibility == "dm_only"
        assert event.actor_id == ctx["owner"]
        # Owner and audit reads retain the full payload.
        assert list_campaign_events(db, ctx["campaign_id"]) != []
        owner_feed = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["owner"])
        assert any(
            e.event_type == ENCOUNTER_ENDED_EVENT and e.id == event.id
            for e in owner_feed
        )
        # Thread members without ownership get no ended event — the secret
        # reason and hidden fate are not recoverable through history.
        member_feed = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["player"])
        assert all(e.event_type != ENCOUNTER_ENDED_EVENT for e in member_feed)
        assert secret_reason not in str([e.payload for e in member_feed])


def test_freeform_post_combat_interaction_still_available():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        _end(db, ctx, encounter)
        submission = accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["player"],
            character_id=ctx["player_pc"],
            raw_content="I search the goblin bodies for anything useful.",
            segments=[{"type": "ic", "text": "I search the goblin bodies for anything useful."}],
            thread_id=ctx["thread_id"],
        )
        assert submission.id is not None


# ── idempotency + follow-up failure isolation ───────────────────────────────


def test_duplicate_end_replays_without_duplication():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        first = _end(db, ctx, encounter, effect_id="fx-end-dup")
        second = _end(db, ctx, encounter, effect_id="fx-end-dup")
        assert second.id == first.id
        assert second.end_outcome == first.end_outcome == "victory"
        assert second.end_operation_id == first.end_operation_id
        assert {h.id for h in list_end_followups(db, encounter.id)} == {
            h.id for h in list_end_followups(db, first.id)
        }
        assert len(list_end_followups(db, encounter.id)) == 5
        # No extra domain event: the inline end stages rows only.
        assert db.get(Encounter, encounter.id).ended_event_id is None


def test_conflicting_end_operation_fails_closed():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        first = _end(db, ctx, encounter, effect_id="fx-end-first")
        with pytest.raises(ValueError, match="already ended"):
            _end(db, ctx, encounter, effect_id="fx-end-second")
        ended = db.get(Encounter, encounter.id)
        assert ended.end_operation_id == first.end_operation_id
        assert ended.end_outcome == "victory"


def test_end_pending_encounter_before_initiative_completes():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start_party(db, ctx, effect_id="fx-start-pending")
        assert encounter.status == "pending_initiative"
        updated = _end(
            db, ctx, encounter, outcome="negotiated_truce",
            reason="Parley succeeds before blades are drawn.",
            effect_id="fx-end-pending",
        )
        assert updated.status == "ended"
        assert updated.end_outcome == "negotiated_truce"
        assert len(list_end_followups(db, encounter.id)) == 5
        assert get_active_encounter(db, ctx["campaign_id"]) is None


def test_inline_dm_effect_end_path():
    """DM structured-effect promotion ends rows flush-only for turn commit."""
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = _npc(db, encounter.id, ctx["goblin_id"])
        campaign = db.get(Campaign, ctx["campaign_id"])
        inline = end_encounter_inline(
            db, campaign, db.get(Encounter, encounter.id),
            {"encounter_id": str(encounter.id), "outcome": "escape",
             "reason": "The goblins melt into the woods.",
             "participant_outcomes": {str(npc.id): "fled"}},
            "fx-end-1",
        )
        assert inline.status == "ended"
        assert inline.end_outcome == "escape"
        assert inline.ended_event_id is None  # staged by the turn commit
        assert len(list_end_followups(db, encounter.id)) == 5
        # Commit the inline rows (the outer turn commit would own this), then
        # run the replay/conflict paths against a second encounter.
        db.commit()
        campaign = db.get(Campaign, ctx["campaign_id"])
        encounter2 = _ready_party(db, ctx, effect_id="fx-start-fx2")
        end_encounter_inline(
            db, campaign, encounter2,
            {"encounter_id": str(encounter2.id), "outcome": "victory",
             "reason": "Done."},
            "fx-end-2",
        )
        same = end_encounter_inline(
            db, campaign, encounter2,
            {"encounter_id": str(encounter2.id), "outcome": "victory",
             "reason": "Done."},
            "fx-end-2",
        )
        assert same.id == encounter2.id
        with pytest.raises(EndEncounterError, match="already ended"):
            end_encounter_inline(
                db, campaign, encounter2,
                {"encounter_id": str(encounter2.id), "outcome": "retreat",
                 "reason": "Other."},
                "fx-end-3",
            )


def test_turn_commit_stages_end_only_for_its_own_end_effects():
    """A DM turn stages encounter.ended only for encounters its effects ended.

    An unrelated ended encounter still missing its lifecycle event must not
    be claimed (or given provenance) by whichever turn commits next.
    """
    fac, ctx = _fixture()
    with fac() as db:
        stray = _ready_party(db, ctx, effect_id="fx-start-stray")
        _end(db, ctx, stray, outcome="retreat", reason="Out of band.",
             effect_id="stray-end")
        db.commit()
        target = _ready_party(db, ctx, effect_id="fx-start-target")
        db.commit()
        _commit_turn_with_effects(db, ctx, [{
            "id": "end-enc-1", "effect_type": "end_encounter",
            "arguments": {"encounter_id": str(target.id), "outcome": "escape",
                          "reason": "The goblins melt into the woods."},
        }])

        target = db.get(Encounter, target.id)
        assert target.status == "ended"
        assert target.ended_event_id is not None
        ended_event = _ended_event(db, ctx, target.id)
        assert target.ended_event_id == ended_event.id
        assert ended_event.provenance["attempt_id"] == str(ctx["attempt_id"])
        stray = db.get(Encounter, stray.id)
        assert stray.ended_event_id is None
        events = list_campaign_events(db, ctx["campaign_id"])
        assert all(
            (e.payload or {}).get("encounter_id") != str(stray.id)
            for e in events if e.event_type == ENCOUNTER_ENDED_EVENT
        )


def test_encounter_ended_observability_counters():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        updated = _end(db, ctx, encounter, effect_id="fx-end-obs")
        assert updated.end_duration_ms is not None and int(updated.end_duration_ms) >= 0
        final = build_final_snapshot(db, db.get(Encounter, encounter.id))
        assert final["round"] >= 1
        assert set(updated.end_participant_outcomes) == {
            str(p.id) for p in db.execute(
                select(EncounterParticipant).where(
                    EncounterParticipant.encounter_id == encounter.id)).scalars().all()
        }
