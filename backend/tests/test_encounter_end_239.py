"""Issue #239 — DM-controlled encounter end and post-combat consequence hooks."""
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
from app.campaigns.replacements import get_lifecycle  # noqa: E402
from app.combat.ending import (  # noqa: E402
    END_OUTCOMES,
    EndEncounterAuthorizationError,
    EndEncounterError,
    build_final_snapshot,
    end_encounter,
    end_encounter_inline,
    find_ended_event,
    list_end_followups,
    process_end_followup,
)
from app.combat.maps import MapError, ensure_map  # noqa: E402
from app.combat.service import (  # noqa: E402
    ENCOUNTER_ENDED_EVENT,
    encounter_view,
    fulfill_human_initiative,
    get_active_encounter,
    roll_npc_initiative,
    start_encounter,
)
from app.combat.turns import TurnError, cast_skip_vote, consume_resource, end_turn  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.post_turn.service import is_post_turn_relevant  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant, EncounterTurnState  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


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


def _revision(db, ctx):
    return int(db.get(Campaign, ctx["campaign_id"]).revision or 0)


def _start_party(db, ctx, *, operation_id="op-start-1", with_npc=False, expected_revision=0):
    parts = [{"character_id": str(ctx["owner_pc"])},
             {"character_id": str(ctx["player_pc"])}]
    if with_npc:
        parts.append({"npc_entity_id": str(ctx["goblin_id"])})
    encounter, _ = start_encounter(
        db, ctx["campaign_id"], operation_id=operation_id, expected_revision=expected_revision,
        actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
        source_attempt_id=ctx["attempt_id"], participants=parts,
    )
    return encounter


def _ready_party(db, ctx, *, with_npc=False, operation_id="op-start-1", expected_revision=0):
    encounter = _start_party(db, ctx, operation_id=operation_id, with_npc=with_npc,
                             expected_revision=expected_revision)
    owner_p = _pc(db, encounter.id, ctx["owner_pc"])
    player_p = _pc(db, encounter.id, ctx["player_pc"])
    fulfill_human_initiative(
        db, encounter.id, owner_p.id, actor_id=ctx["owner"],
        payload={"source": "app", "raw_rolls": [10],
                 "modifier": owner_p.initiative_modifier,
                 "total": 10 + owner_p.initiative_modifier},
    )
    _, _, _, encounter, event = fulfill_human_initiative(
        db, encounter.id, player_p.id, actor_id=ctx["player"],
        payload={"source": "app", "raw_rolls": [12],
                 "modifier": player_p.initiative_modifier,
                 "total": 12 + player_p.initiative_modifier},
    )
    if with_npc:
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        _, encounter, event = roll_npc_initiative(db, encounter.id, npc.id, raw_d20=7)
    assert event is not None or with_npc is False
    return db.get(Encounter, encounter.id)


def _end(db, ctx, encounter, **kwargs):
    params = {
        "actor_id": ctx["owner"],
        "outcome": "victory",
        "reason": "The goblins are slain.",
        "expected_revision": _revision(db, ctx),
        "operation_id": "op-end-1",
    }
    params.update(kwargs)
    return end_encounter(db, encounter.id, **params)


# ── supported fictional reasons (not only all-enemies-dead) ─────────────────


@pytest.mark.parametrize("outcome", sorted(END_OUTCOMES))
def test_end_supported_outcomes(outcome):
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        rev_before = _revision(db, ctx)
        updated, event, hooks = _end(
            db, ctx, encounter, outcome=outcome,
            reason=f"Combat resolves: {outcome}.",
            operation_id=f"op-end-{outcome}",
        )
        assert updated.status == "ended"
        assert updated.end_outcome == outcome
        assert updated.ended_event_id == event.id
        assert event.event_type == ENCOUNTER_ENDED_EVENT
        assert event.sequence == int(db.get(Campaign, ctx["campaign_id"]).revision)
        assert int(db.get(Campaign, ctx["campaign_id"]).revision) == rev_before + 1
        assert {h.hook_type for h in hooks} == {
            "loot_availability", "xp_progression", "death_aftermath",
            "custody_state", "post_turn_consolidation",
        }
        assert all(h.status == "pending" for h in hooks)


def test_kill_based_end_with_fleeing_enemy():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        updated, event, _ = _end(
            db, ctx, encounter, outcome="victory",
            reason="Two goblins slain; the last flees into the treeline.",
            participant_outcomes={str(npc.id): "fled"},
        )
        assert updated.status == "ended"
        assert updated.end_participant_outcomes[str(npc.id)] == "fled"
        assert event.payload["participant_outcomes"][str(npc.id)] == "fled"


def test_surrender_end_with_custody_outcomes():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True)
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        updated, _, _ = _end(
            db, ctx, encounter, outcome="surrender",
            reason="The goblin drops its blade and yields.",
            participant_outcomes={str(npc.id): "surrendered"},
        )
        assert updated.end_outcome == "surrender"
        assert updated.end_participant_outcomes[str(npc.id)] == "surrendered"
        hooks = {h.hook_type: h for h in list_end_followups(db, encounter.id)}
        row = process_end_followup(
            db, encounter.id, "custody_state",
            result={"captives": [str(npc.id)], "held_by": "party"},
        )
        assert row.status == "complete"
        assert row.result["captives"] == [str(npc.id)]
        assert hooks["custody_state"].id == row.id


def test_party_retreat_end():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        updated, event, _ = _end(
            db, ctx, encounter, outcome="retreat",
            reason="The party breaks off and falls back to the road.",
            participant_outcomes={
                str(owner_p.id): "retreated", str(player_p.id): "retreated",
            },
        )
        assert updated.end_outcome == "retreat"
        assert event.payload["outcome"] == "retreat"


def test_capture_end():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        player_p = _pc(db, encounter.id, ctx["player_pc"])
        updated, _, _ = _end(
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
        updated, event, _ = _end(
            db, ctx, encounter, outcome="defeat",
            reason="The owlbear crushes the scout.",
            participant_outcomes={str(player_p.id): "slain"},
        )
        assert updated.status == "ended"
        lifecycle = get_lifecycle(db, ctx["campaign_id"], ctx["player_pc"])
        assert lifecycle is not None and lifecycle.status == "dead"
        assert event.payload["declared_deaths"] == [str(ctx["player_pc"])]
        # Death aftermath hook completes through normal campaign processing.
        row = process_end_followup(
            db, encounter.id, "death_aftermath",
            result={"dead": [str(ctx["player_pc"])], "replacement_eligible": True},
        )
        assert row.status == "complete"


def test_end_validates_outcome_reason_and_participants():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        rev = _revision(db, ctx)
        with pytest.raises(EndEncounterError, match="outcome must be"):
            end_encounter(db, encounter.id, actor_id=ctx["owner"], outcome="everyone-wins",
                          reason="Nope.", expected_revision=rev, operation_id="op-bad-1")
        with pytest.raises(EndEncounterError, match="reason is required"):
            end_encounter(db, encounter.id, actor_id=ctx["owner"], outcome="victory",
                          reason="  ", expected_revision=rev, operation_id="op-bad-2")
        with pytest.raises(EndEncounterError, match="unknown participants"):
            end_encounter(db, encounter.id, actor_id=ctx["owner"], outcome="victory",
                          reason="Done.", participant_outcomes={str(uuid.uuid4()): "fled"},
                          expected_revision=rev, operation_id="op-bad-3")
        # Failed end transactions leave the encounter active, never half-closed.
        assert db.get(Encounter, encounter.id).status == "active"
        assert list_end_followups(db, encounter.id) == []
        assert find_ended_event(db, db.get(Encounter, encounter.id)) is None


def test_end_requires_owner_dm_path():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        with pytest.raises(EndEncounterAuthorizationError):
            end_encounter(db, encounter.id, actor_id=ctx["player"], outcome="victory",
                          reason="A player cannot declare the end.",
                          expected_revision=_revision(db, ctx), operation_id="op-nope-1")
        assert db.get(Encounter, encounter.id).status == "active"


# ── frozen progression + preserved final state ──────────────────────────────


def test_ending_closes_turn_reaction_progression():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        seq = int(encounter.turn_sequence or 0)
        rev = _revision(db, ctx)
        _end(db, ctx, encounter, expected_revision=rev)
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


def test_ending_blocks_movement_and_pending_initiative_paths():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-m")
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        ensure_map(db, encounter.id, actor_id=ctx["owner"], width=8, height=8,
                   placements=[{"participant_id": str(owner_p.id), "col": 1, "row": 1}],
                   expected_revision=_revision(db, ctx), operation_id="op-map-1")
        _end(db, ctx, encounter, expected_revision=_revision(db, ctx),
             operation_id="op-end-m")
        from app.combat.maps import move_participant

        with pytest.raises(MapError, match="active encounter"):
            move_participant(db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                             to_col=2, to_row=2,
                             expected_turn_sequence=int(encounter.turn_sequence or 0),
                             expected_revision=_revision(db, ctx),
                             operation_id="op-move-after-end")
        # Initiative paths also fail closed once ended.
        with pytest.raises(Exception, match="status|ended|initiative"):
            roll_npc_initiative(
                db, encounter.id,
                db.execute(select(EncounterParticipant).where(
                    EncounterParticipant.encounter_id == encounter.id,
                    EncounterParticipant.npc_entity_id == ctx["goblin_id"])).scalars().one().id,
                raw_d20=10,
            )


def test_final_state_preserved_for_history_and_scene_play():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        # Spend the active PC's action so the frozen budget is observable.
        consume_resource(db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                         resource="action",
                         expected_turn_sequence=int(encounter.turn_sequence or 0))
        _, event, _ = _end(db, ctx, encounter, expected_revision=_revision(db, ctx))
        final = event.payload["final_state"]
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
        # builder agrees with the committed payload.
        rebuilt = build_final_snapshot(db, db.get(Encounter, encounter.id))
        assert rebuilt["participant_count"] == 2


def test_ended_event_feeds_history_and_post_turn():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        _, event, _ = _end(db, ctx, encounter, expected_revision=_revision(db, ctx))
        events = list_campaign_events(db, ctx["campaign_id"])
        kinds = [e.event_type for e in events]
        assert ENCOUNTER_ENDED_EVENT in kinds
        ended_row = next(e for e in events if e.event_type == ENCOUNTER_ENDED_EVENT)
        assert ended_row.sequence == int(db.get(Campaign, ctx["campaign_id"]).revision)
        assert ended_row.payload["outcome"] == "victory"
        assert "final_state" in ended_row.payload
        # The ended event is post-turn relevant: nothing accepted is dropped.
        assert is_post_turn_relevant(ENCOUNTER_ENDED_EVENT) is True
        # Thread-scoped like the other lifecycle events.
        from app.combat.service import THREAD_SCOPED_EVENT_TYPES

        assert ENCOUNTER_ENDED_EVENT in THREAD_SCOPED_EVENT_TYPES
        assert ended_row.payload["thread_id"] == ctx["thread_id"]


def test_hidden_enemy_final_data_stays_scoped():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-h")
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        _, event, _ = _end(
            db, ctx, encounter, expected_revision=_revision(db, ctx),
            operation_id="op-end-h",
            participant_outcomes={str(npc.id): "slain"},
        )
        # Player projection never carries hidden NPC stat breakdowns.
        player_view = encounter_view(
            db, db.get(Encounter, encounter.id), ctx["player"], is_owner=False)
        goblin_view = next(p for p in player_view["participants"]
                           if p.get("npc_entity_id") == str(ctx["goblin_id"]))
        assert "initiative_modifier" not in goblin_view
        assert "raw_roll" not in goblin_view
        # Ended-event snapshot flags the redaction instead of leaking HP.
        final_npc = next(p for p in event.payload["final_state"]["participants"]
                         if p["id"] == str(npc.id))
        assert final_npc["hit_points"] is None
        assert final_npc["detail_redacted"] is True
        # Turn projection is closed; map/final positions still readable.
        assert player_view["turn"] is None


def test_non_owner_end_projection_redacts_reason_and_hidden_fates():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-redact")
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        assert npc.stat_visibility == "dm_private"
        owner_pc = _pc(db, encounter.id, ctx["owner_pc"])
        secret_reason = "The hidden ambusher slips away with the stolen seal."
        _end(
            db, ctx, encounter, expected_revision=_revision(db, ctx),
            operation_id="op-end-redact", outcome="escape", reason=secret_reason,
            participant_outcomes={str(npc.id): "fled", str(owner_pc.id): "standing"},
        )
        ended = db.get(Encounter, encounter.id)
        owner_view = encounter_view(db, ended, ctx["owner"], is_owner=True)
        assert owner_view["end_outcome"] == "escape"
        assert owner_view["end_reason"] == secret_reason
        assert owner_view["end_participant_outcomes"][str(npc.id)] == "fled"
        player_view = encounter_view(db, ended, ctx["player"], is_owner=False)
        assert player_view["end_outcome"] == "escape"
        assert player_view["end_reason"] is None
        assert secret_reason not in str(player_view)
        assert str(npc.id) not in (player_view["end_participant_outcomes"] or {})
        assert player_view["end_participant_outcomes"][str(owner_pc.id)] == "standing"


def test_ended_event_hidden_from_member_history():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-evh")
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        secret_reason = "The hidden ambusher slips away with the stolen seal."
        _, event, _ = _end(
            db, ctx, encounter, expected_revision=_revision(db, ctx),
            operation_id="op-end-evh", outcome="escape", reason=secret_reason,
            participant_outcomes={str(npc.id): "fled"},
        )
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
        _end(db, ctx, encounter, expected_revision=_revision(db, ctx))
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
        rev = _revision(db, ctx)
        first, first_event, first_hooks = _end(
            db, ctx, encounter, expected_revision=rev, operation_id="op-end-dup")
        rev_after = _revision(db, ctx)
        assert rev_after == rev + 1
        second, second_event, second_hooks = _end(
            db, ctx, encounter, expected_revision=rev_after, operation_id="op-end-dup")
        assert second.id == first.id
        assert second_event.id == first_event.id
        assert {h.id for h in second_hooks} == {h.id for h in first_hooks}
        assert len(list_end_followups(db, encounter.id)) == 5
        assert int(db.get(Encounter, encounter.id).duplicate_end_count) == 1
        # No extra revision bump, no extra domain event.
        assert _revision(db, ctx) == rev_after
        assert sum(1 for e in list_campaign_events(db, ctx["campaign_id"])
                   if e.event_type == ENCOUNTER_ENDED_EVENT) == 1


def test_conflicting_end_operation_fails_closed():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        _end(db, ctx, encounter, operation_id="op-end-first")
        with pytest.raises(EndEncounterError, match="already ended"):
            _end(db, ctx, encounter, operation_id="op-end-second")
        assert db.get(Encounter, encounter.id).end_operation_id == "op-end-first"
        assert db.get(Encounter, encounter.id).end_outcome == "victory"


def test_failed_followup_does_not_reopen_or_invalidate():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        _end(db, ctx, encounter)
        failed = process_end_followup(
            db, encounter.id, "xp_progression",
            fail_reason="reward job crashed: division by zero in XP split",
        )
        assert failed.status == "failed"
        assert failed.attempts == 1
        ended = db.get(Encounter, encounter.id)
        assert ended.status == "ended"
        assert ended.end_outcome == "victory"
        assert int(ended.followup_failure_count) == 1
        # The other hooks are untouched; the failed hook retries cleanly.
        statuses = {h.hook_type: h.status for h in list_end_followups(db, encounter.id)}
        assert statuses["xp_progression"] == "failed"
        assert statuses["loot_availability"] == "pending"
        retried = process_end_followup(
            db, encounter.id, "xp_progression",
            result={"awarded_xp": {}, "note": "DM awards XP manually"},
        )
        assert retried.status == "complete"
        assert retried.attempts == 2
        assert db.get(Encounter, encounter.id).status == "ended"
        # Completed hooks replay instead of re-applying.
        replay = process_end_followup(
            db, encounter.id, "xp_progression",
            result={"awarded_xp": {"x": 1}},
        )
        assert replay.id == retried.id
        assert replay.result == {"awarded_xp": {}, "note": "DM awards XP manually"}


def test_followup_validation_and_unknown_hooks():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        with pytest.raises(EndEncounterError, match="ended encounter"):
            process_end_followup(db, encounter.id, "loot_availability", result={})
        _end(db, ctx, encounter)
        with pytest.raises(EndEncounterError, match="hook_type must be"):
            process_end_followup(db, encounter.id, "grant_castle", result={})
        with pytest.raises(EndEncounterError, match="must be an object"):
            process_end_followup(db, encounter.id, "loot_availability", result=["gold"])
        with pytest.raises(EndEncounterError, match="not found"):
            process_end_followup(db, uuid.uuid4(), "loot_availability", result={})
        # A hook row deleted out-of-band reads as missing, never recreated here.
        doomed = next(h for h in list_end_followups(db, encounter.id)
                      if h.hook_type == "loot_availability")
        db.delete(doomed)
        db.flush()
        with pytest.raises(EndEncounterError, match="no loot_availability hook"):
            process_end_followup(db, encounter.id, "loot_availability", result={})


def test_end_pending_encounter_before_initiative_completes():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start_party(db, ctx, operation_id="op-start-pending")
        assert encounter.status == "pending_initiative"
        updated, event, hooks = _end(
            db, ctx, encounter, outcome="negotiated_truce",
            reason="Parley succeeds before blades are drawn.",
            operation_id="op-end-pending",
        )
        assert updated.status == "ended"
        assert event.event_type == ENCOUNTER_ENDED_EVENT
        assert len(hooks) == 5
        assert get_active_encounter(db, ctx["campaign_id"]) is None


def test_inline_dm_effect_end_path():
    """DM structured-effect promotion ends rows flush-only for turn commit."""
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-fx")
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
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
        encounter2 = _ready_party(db, ctx, operation_id="op-start-fx2",
                                  expected_revision=_revision(db, ctx))
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


def test_encounter_ended_observability_counters():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx)
        updated, event, _ = _end(db, ctx, encounter, operation_id="op-end-obs")
        assert updated.end_duration_ms is not None and int(updated.end_duration_ms) >= 0
        assert event.payload["duration_ms"] >= 0
        assert event.payload["round"] >= 1
        assert set(event.payload["participant_outcomes"]) == {
            str(p.id) for p in db.execute(
                select(EncounterParticipant).where(
                    EncounterParticipant.encounter_id == encounter.id)).scalars().all()
        }


def test_http_end_followups_owner_only(monkeypatch):
    from fastapi.testclient import TestClient

    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    fac, ctx = _fixture()
    with fac() as db:
        encounter = _ready_party(db, ctx, with_npc=True, operation_id="op-start-fol")
        npc = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        _end(
            db, ctx, encounter, expected_revision=_revision(db, ctx),
            operation_id="op-end-fol", outcome="surrender",
            reason="The goblin yields.",
            participant_outcomes={str(npc.id): "surrendered"},
        )
        process_end_followup(
            db, encounter.id, "custody_state",
            result={"captives": [str(npc.id)], "held_by": "party"},
        )
        db.commit()
        encounter_id = str(encounter.id)
        campaign_id = str(ctx["campaign_id"])
        owner_id, player_id = str(ctx["owner"]), str(ctx["player"])
        npc_id = str(npc.id)

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, uuid.UUID(request.headers["x-test-user"]))

    monkeypatch.setattr("app.combat.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        as_owner = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-followups",
            headers={"x-test-user": owner_id},
        )
        assert as_owner.status_code == 200, as_owner.text
        custody = next(
            h for h in as_owner.json()["followups"] if h["hook_type"] == "custody_state"
        )
        assert custody["result"]["captives"] == [npc_id]
        as_player = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/end-followups",
            headers={"x-test-user": player_id},
        )
        assert as_player.status_code == 403, as_player.text
        assert npc_id not in as_player.text
    finally:
        app.dependency_overrides.clear()
