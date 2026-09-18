"""Issue #230 — authoritative encounter lifecycle, participants, initiative, rounds, active turn."""
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
    ENCOUNTER_READY_EVENT,
    ENCOUNTER_STARTED_EVENT,
    EncounterAlreadyActiveError,
    EncounterAuthorizationError,
    EncounterError,
    EncounterNotReadyError,
    can_view_encounter,
    compute_turn_order,
    encounter_view,
    fulfill_human_initiative,
    get_active_encounter,
    get_active_participant,
    get_snapshot_encounter,
    get_turn_order,
    list_participants,
    roll_npc_initiative,
    start_encounter,
)
from app.dm.turns import coordinate_turn  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant  # noqa: E402
from models.dm import PlayerRollFulfillment, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


def _engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _seed_world(db, *, second_pc=True, npc=True):
    owner = uuid.uuid4()
    player = uuid.uuid4()
    campaign_id = uuid.uuid4()
    owner_pc = uuid.uuid4()
    player_pc = uuid.uuid4()
    absent_pc = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=player, email="player@example.com"),
        Campaign(id=campaign_id, owner_id=owner, name="Encounter Table", revision=0),
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
    if second_pc:
        db.add(Character(id=absent_pc, owner_id=player, name="Absent Mage", system="dnd5e"))
    # Dex 16 (+3) +1 bonus = +4; Dex 14 (+2) +0 = +2; Dex 10 = +0.
    db.add_all([
        Dnd5eCharacterSheet(character_id=owner_pc, owner_id=owner, character_name="Owner Blade",
                            dexterity=16, initiative_bonus=1, level=3),
        Dnd5eCharacterSheet(character_id=player_pc, owner_id=player, character_name="Player Bow",
                            dexterity=14, initiative_bonus=0, level=3),
    ])
    if second_pc:
        db.add(Dnd5eCharacterSheet(character_id=absent_pc, owner_id=player, character_name="Absent Mage",
                                   dexterity=10, initiative_bonus=0, level=3))
    goblin_id = None
    if npc:
        goblin = WorldEntity(campaign_id=campaign_id, entity_type="npc", name="Goblin Ambusher",
                             visibility="dm_only",
                             details={"initiative_modifier": 2, "dex_modifier": 1})
        db.add(goblin)
        db.flush()
        goblin_id = goblin.id
    db.commit()
    thread = get_or_create_campaign_thread(db, campaign_id, created_by=owner)
    submission = accept_submission(
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
        "owner_pc": owner_pc, "player_pc": player_pc, "absent_pc": absent_pc,
        "goblin_id": goblin_id, "turn_id": turn.id, "attempt_id": attempt.id,
    }


def _fixture():
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db)
    return fac, ctx


def _start(db, ctx, participants, *, operation_id="op-enc-1", revision=0, scene=None, attempt_id=None):
    return start_encounter(
        db, ctx["campaign_id"], operation_id=operation_id, expected_revision=revision,
        actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
        source_attempt_id=attempt_id or ctx["attempt_id"],
        scene=scene or {"location_name": "Treeline"}, participants=participants,
    )


def _pc_participant(db, encounter_id, character_id):
    return db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter_id,
            EncounterParticipant.character_id == character_id,
        )
    ).scalars().one()


def _fulfill(db, encounter_id, participant, actor, raw):
    return fulfill_human_initiative(
        db, encounter_id, participant.id, actor_id=actor,
        payload={"source": "app", "raw_rolls": [raw],
                 "modifier": participant.initiative_modifier,
                 "total": raw + participant.initiative_modifier},
    )


# ── selection: solo + absent-PC exclusion ───────────────────────────────────


def test_solo_start_selects_only_listed_pc_and_emits_start_event():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, event = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
        assert encounter.status == "pending_initiative"
        assert encounter.round == 1
        assert encounter.revision == 1
        assert encounter.start_source == "api"
        assert encounter.participant_count == 1
        assert encounter.active_participant_id is None
        assert event.event_type == ENCOUNTER_STARTED_EVENT
        assert event.sequence == 1
        assert db.get(Campaign, ctx["campaign_id"]).revision == 1
        parts = list_participants(db, encounter.id)
        assert len(parts) == 1
        assert parts[0].kind == "pc"
        assert parts[0].controller_user_id == ctx["owner"]
        assert parts[0].initiative_modifier == 4  # dex 16 + bonus 1
        assert parts[0].dex_modifier == 3
        assert parts[0].initiative_status == "pending"
        assert parts[0].roll_request_id is not None
        # Absent PC is never auto-included: no participant, no roll request.
        assert db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.character_id == ctx["absent_pc"],
            )
        ).scalars().first() is None
        assert db.execute(
            select(PlayerRollRequest).where(PlayerRollRequest.character_id == ctx["absent_pc"])
        ).scalars().first() is None
        # Roll request rides the #204 record with initiative kind.
        request = db.get(PlayerRollRequest, parts[0].roll_request_id)
        assert request.roll_kind == "initiative"
        assert request.status == "pending"
        assert "dc_private" not in request.to_dict()


def test_multiplayer_start_includes_only_selected_pcs():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(
            db, ctx,
            [{"character_id": str(ctx["owner_pc"])}, {"character_id": str(ctx["player_pc"])}],
        )
        assert encounter.participant_count == 2
        keys = sorted(p.participant_key for p in list_participants(db, encounter.id))
        assert keys == sorted([f"pc:{ctx['owner_pc']}", f"pc:{ctx['player_pc']}"])


def test_duplicate_selection_is_rejected_never_duplicated():
    fac, ctx = _fixture()
    with fac() as db:
        with pytest.raises(EncounterError, match="duplicate"):
            _start(db, ctx, [
                {"character_id": str(ctx["owner_pc"])},
                {"character_id": str(ctx["owner_pc"])},
            ])
        # Failed validation leaves no half-created encounter behind.
        assert get_active_encounter(db, ctx["campaign_id"]) is None


def test_second_start_while_pending_fails_closed():
    fac, ctx = _fixture()
    with fac() as db:
        _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
        with pytest.raises(EncounterAlreadyActiveError):
            _start(db, ctx, [{"character_id": str(ctx["player_pc"])}],
                   operation_id="op-enc-2", revision=1)


# ── human + NPC initiative, ordering, readiness ─────────────────────────────


def test_human_and_npc_initiative_complete_to_active_with_order():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},   # +4
            {"character_id": str(ctx["player_pc"])},  # +2
            {"npc_entity_id": str(ctx["goblin_id"])},  # +2
        ])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        goblin_p = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        assert goblin_p.roll_request_id is None  # runtime path, no #204 request

        # NPC rolls first: encounter stays pending, humans never auto-filled.
        _, encounter, ready_event = roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=15)
        assert encounter.status == "pending_initiative"
        assert ready_event is None
        assert db.get(EncounterParticipant, goblin_p.id).initiative_total == 17
        assert db.get(EncounterParticipant, goblin_p.id).roll_source == "dm_runtime"

        # Humans roll their own dice.
        _fulfill(db, encounter.id, owner_p, ctx["owner"], 10)  # total 14
        _, _, _, encounter, ready_event = _fulfill(db, encounter.id, player_p, ctx["player"], 18)  # total 20
        assert encounter.status == "active"
        assert encounter.round == 1
        assert ready_event is not None
        assert ready_event.event_type == ENCOUNTER_READY_EVENT

        order = get_turn_order(db, encounter.id)
        # Player 20 first, goblin 17 second, owner 14 third.
        assert [str(p.character_id) if p.character_id else "goblin" for p in order] == \
            [str(ctx["player_pc"]), "goblin", str(ctx["owner_pc"])]
        assert [p.sort_order for p in order] == [0, 1, 2]
        active = get_active_participant(db, encounter.id)
        assert active.id == order[0].id
        assert encounter.active_participant_id == active.id
        assert encounter.initiative_wait_ms is not None and encounter.initiative_wait_ms >= 0
        assert encounter.time_to_first_turn_ms == encounter.initiative_wait_ms
        assert encounter.roll_sources == {
            str(owner_p.id): "human_app", str(player_p.id): "human_app", str(goblin_p.id): "dm_runtime",
        }
        assert "total_desc" in (encounter.tie_resolution or "")
        # Both domain events exist in campaign order.
        types = [e.event_type for e in list_campaign_events(db, ctx["campaign_id"])]
        assert types == [ENCOUNTER_STARTED_EVENT, ENCOUNTER_READY_EVENT]


def test_runtime_roll_refuses_human_pc_and_human_path_refuses_npc():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        goblin_p = db.execute(
            select(EncounterParticipant).where(EncounterParticipant.encounter_id == encounter.id,
                                               EncounterParticipant.kind == "npc")
        ).scalars().one()
        with pytest.raises(EncounterError, match="humans roll their own"):
            roll_npc_initiative(db, encounter.id, owner_p.id, raw_d20=12)
        with pytest.raises(EncounterError, match="runtime roll path"):
            fulfill_human_initiative(
                db, encounter.id, goblin_p.id, actor_id=ctx["owner"],
                payload={"source": "app", "raw_rolls": [10], "modifier": 2, "total": 12},
            )
        with pytest.raises(EncounterError, match="between 1 and 20"):
            roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=99)


def test_tie_handling_is_deterministic_2024():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},   # dex +3, mod +4
            {"character_id": str(ctx["player_pc"])},  # dex +2, mod +2
        ])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        # Full tie: owner 10+4=14 (dex 3), player 12+2=14 (dex 2) → dex breaks it.
        _fulfill(db, encounter.id, owner_p, ctx["owner"], 10)
        _, _, _, encounter, _ = _fulfill(db, encounter.id, player_p, ctx["player"], 12)
        order = get_turn_order(db, encounter.id)
        assert [p.id for p in order] == [owner_p.id, player_p.id]  # higher dex first
        assert not any(p.is_tied for p in order)

    fac2, ctx2 = _fixture()
    with fac2() as db:
        # Exact tie on total AND dex needs equal sheets; force via compute_turn_order.
        encounter, _ = _start(db, ctx2, [{"character_id": str(ctx2["owner_pc"])}])
        parts = list_participants(db, encounter.id)
        a, b = parts[0], EncounterParticipant(
            encounter_id=encounter.id, campaign_id=ctx2["campaign_id"],
            participant_key="pc:tie-break", kind="pc", display_name="Tie",
            initiative_modifier=4, dex_modifier=3,
            initiative_total=14, initiative_status="fulfilled",
        )
        a.initiative_total = 14
        a.initiative_status = "fulfilled"
        ordered, tied_groups = compute_turn_order([a, b])
        assert tied_groups == 1
        # Deterministic final breaker: participant id ascending.
        expect_first = min(str(a.id), str(b.id))
        assert str(ordered[0]) == expect_first
        assert ordered == compute_turn_order([b, a])[0]  # input order irrelevant


def test_incomplete_initiative_stays_pending_without_guessing():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ])
        goblin_p = db.execute(
            select(EncounterParticipant).where(EncounterParticipant.encounter_id == encounter.id,
                                               EncounterParticipant.kind == "npc")
        ).scalars().one()
        roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=15)
        encounter = db.get(Encounter, encounter.id)
        assert encounter.status == "pending_initiative"
        assert encounter.active_participant_id is None
        with pytest.raises(EncounterNotReadyError):
            get_turn_order(db, encounter.id)
        with pytest.raises(EncounterNotReadyError):
            get_active_participant(db, encounter.id)
        # The human roll was never invented: still pending, no fulfillment row.
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        assert owner_p.initiative_status == "pending"
        assert owner_p.initiative_total is None
        assert db.get(PlayerRollFulfillment, owner_p.roll_request_id) is None
        assert db.execute(
            select(PlayerRollFulfillment).where(
                PlayerRollFulfillment.roll_request_id == owner_p.roll_request_id)
        ).scalars().first() is None


def test_ready_operation_key_bounded_for_max_length_start_key():
    from models.reliability import Outbox

    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(
            db, ctx, [{"character_id": str(ctx["owner_pc"])}], operation_id="o" * 128,
        )
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        _, _, _, encounter, event = _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
        assert encounter.status == "active"
        assert event is not None
        assert event.operation_id == f"encounter:{encounter.id}:initiative-ready"
        assert len(event.operation_id) <= 128
        outbox_keys = {
            row.operation_id for row in db.execute(
                select(Outbox).where(Outbox.campaign_id == ctx["campaign_id"])
            ).scalars().all()
        }
        assert f"encounter:{encounter.id}:initiative-ready" in outbox_keys
        assert all(len(key or "") <= 128 for key in outbox_keys)


def test_npc_override_preserves_canonical_dex_tiebreak():
    fac, ctx = _fixture()
    with fac() as db:
        brute = WorldEntity(
            campaign_id=ctx["campaign_id"], entity_type="monster", name="Ogre Brute",
            visibility="dm_only", details={"dex_modifier": 3},
        )
        db.add(brute)
        db.commit()
        # NPC total modifier overridden to +5, but canonical Dex +3 survives
        # for tiebreaks: 9+5=14 ties the PC's 12+2=14 (Dex +2) → NPC first.
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["player_pc"])},
            {"npc_entity_id": str(brute.id), "initiative_modifier": 5},
        ])
        npc_p = next(p for p in list_participants(db, encounter.id) if p.kind == "monster")
        assert npc_p.initiative_modifier == 5
        assert npc_p.dex_modifier == 3
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        roll_npc_initiative(db, encounter.id, npc_p.id, raw_d20=9)
        _, _, _, encounter, _ = _fulfill(db, encounter.id, player_p, ctx["player"], 12)
        order = get_turn_order(db, encounter.id)
        assert [p.id for p in order] == [npc_p.id, player_p.id]


# ── idempotency / duplicates ────────────────────────────────────────────────

def test_duplicate_start_and_duplicate_fulfill_are_idempotent():
    fac, ctx = _fixture()
    with fac() as db:
        first, first_event = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"character_id": str(ctx["player_pc"])},
        ])
        replay, replay_event = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"character_id": str(ctx["player_pc"])},
        ])
        assert replay.id == first.id
        assert replay_event is not None and replay_event.id == first_event.id
        assert len(list_participants(db, first.id)) == 2
        assert len(db.execute(
            select(PlayerRollRequest).where(PlayerRollRequest.campaign_id == ctx["campaign_id"])
        ).scalars().all()) == 2

        owner_p = _pc_participant(db, first.id, ctx["owner_pc"])
        player_p = _pc_participant(db, first.id, ctx["player_pc"])
        # Wrong human cannot roll for another PC.
        with pytest.raises(EncounterAuthorizationError):
            _fulfill(db, first.id, owner_p, ctx["player"], 10)
        # Arithmetic is code-owned: lying totals/modifiers rejected.
        with pytest.raises(EncounterError, match="must equal d20"):
            fulfill_human_initiative(
                db, first.id, owner_p.id, actor_id=ctx["owner"],
                payload={"source": "app", "raw_rolls": [10],
                         "modifier": owner_p.initiative_modifier, "total": 999},
            )
        _fulfill(db, first.id, owner_p, ctx["owner"], 10)
        # Duplicate fulfillment of the same request cannot apply twice.
        with pytest.raises(EncounterError, match="cannot be fulfilled"):
            _fulfill(db, first.id, owner_p, ctx["owner"], 10)
        assert len(db.execute(select(PlayerRollFulfillment)).scalars().all()) == 1
        # Retry preserves already submitted initiative: PC1 still fulfilled.
        assert db.get(EncounterParticipant, owner_p.id).initiative_status == "fulfilled"
        _fulfill(db, first.id, player_p, ctx["player"], 12)
        assert db.get(Encounter, first.id).status == "active"


def test_npc_reroll_replay_is_idempotent_but_conflicts_rejected():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"npc_entity_id": str(ctx["goblin_id"])},
            {"character_id": str(ctx["owner_pc"])},  # stays pending: replay is not terminal
        ])
        goblin_p = next(p for p in list_participants(db, encounter.id) if p.kind == "npc")
        first_p, _, _ = roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=15)
        replay_p, _, _ = roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=15)
        assert replay_p.initiative_total == first_p.initiative_total == 17
        with pytest.raises(EncounterError, match="already recorded"):
            roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=3)
        # Malformed replay input stays inside the validation contract (422),
        # never escaping as an uncaught ValueError (500).
        with pytest.raises(EncounterError, match="between 1 and 20"):
            roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20="not-a-number")


def test_off_roster_and_terminal_pcs_are_rejected():
    from models.campaigns import CampaignPcLifecycle

    fac, ctx = _fixture()
    with fac() as db:
        # absent_pc belongs to a member but is not selected: off-roster.
        with pytest.raises(EncounterError, match="active roster"):
            _start(db, ctx, [{"character_id": str(ctx["absent_pc"])}],
                   operation_id="op-off-roster")
            db.rollback()
        # A selected PC with a terminal lifecycle cannot join combat.
        db.add(CampaignPcLifecycle(
            campaign_id=ctx["campaign_id"], character_id=ctx["player_pc"],
            user_id=ctx["player"], status="dead",
        ))
        db.commit()
        with pytest.raises(EncounterError, match="is dead"):
            _start(db, ctx, [{"character_id": str(ctx["player_pc"])}],
                   operation_id="op-dead-pc")
            db.rollback()
        assert get_active_encounter(db, ctx["campaign_id"]) is None


# ── reconnect / restart durability ──────────────────────────────────────────


def test_state_survives_reconnect_and_restart(tmp_path):
    path = tmp_path / "encounter.db"
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db)
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ])
        encounter_id = encounter.id
        goblin_id = next(p.id for p in list_participants(db, encounter_id) if p.kind == "npc")
        roll_npc_initiative(db, encounter_id, goblin_id, raw_d20=15)
    eng.dispose()  # simulate process restart: all sessions gone

    eng2 = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    fac2 = sessionmaker(bind=eng2, expire_on_commit=False)
    with fac2() as db:
        revived = get_active_encounter(db, ctx["campaign_id"])
        assert revived is not None and revived.id == encounter_id
        assert revived.status == "pending_initiative"
        parts = list_participants(db, encounter_id)
        assert len(parts) == 2
        goblin = next(p for p in parts if p.kind == "npc")
        assert goblin.initiative_total == 17  # NPC roll survived
        human = next(p for p in parts if p.kind == "pc")
        assert human.initiative_status == "pending"
        pending_request = db.get(PlayerRollRequest, human.roll_request_id)
        assert pending_request is not None and pending_request.status == "pending"
        # Fulfillment works after restart and completes the encounter.
        _fulfill(db, encounter_id, human, ctx["owner"], 12)
        assert db.get(Encounter, encounter_id).status == "active"
        assert len(get_turn_order(db, encounter_id)) == 2


# ── privacy: hidden NPC stats stay DM-private ───────────────────────────────


def test_hidden_npc_stats_stay_dm_private():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [
            {"character_id": str(ctx["player_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ])
        owner_view = encounter_view(db, encounter, ctx["owner"], is_owner=True)
        member_view = encounter_view(db, encounter, ctx["player"], is_owner=False)
        npc_owner = next(p for p in owner_view["participants"] if p["kind"] == "npc")
        npc_member = next(p for p in member_view["participants"] if p["kind"] == "npc")
        assert npc_owner["initiative_modifier"] == 2
        assert npc_owner["stat_source"]["source_type"] == "world_entity_details"
        assert "initiative_modifier" not in npc_member
        assert "stat_source" not in npc_member
        assert "raw_roll" not in npc_member
        assert "roll_request_id" not in npc_member
        # Turn order totals remain visible (needed to display order).
        assert "initiative_total" in npc_member
        # Members see their own PC's private roll linkage; others do not.
        own_pc = next(p for p in member_view["participants"] if p["kind"] == "pc")
        assert own_pc["roll_request_id"] is not None
        # Snapshot projection carries the filtered encounter (reconnect path).
        snapshot_encounter = get_snapshot_encounter(db, ctx["campaign_id"], ctx["player"])
        assert snapshot_encounter is not None
        assert snapshot_encounter["status"] == "pending_initiative"
        assert snapshot_encounter["my_pending_initiative"] == [own_pc["id"]]
        # Non-members get no encounter projection.
        assert get_snapshot_encounter(db, ctx["campaign_id"], uuid.uuid4()) is None


# ── DM structured effect path ───────────────────────────────────────────────


def test_dm_structured_effect_starts_encounter_inline():
    from app.dm.contract import normalize_contract
    from app.dm.effects import apply_staged_effects
    from models.dm import DmTurn, DmTurnAttempt

    fac, ctx = _fixture()
    with fac() as db:
        campaign = db.get(Campaign, ctx["campaign_id"])
        turn = db.get(DmTurn, ctx["turn_id"])
        attempt = db.get(DmTurnAttempt, ctx["attempt_id"])
        contract = normalize_contract({
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond",
            "reason": "combat begins",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "Goblins attack!", "claim_kind": "observation",
                            "origin": "dm_adjudication"}],
            }],
            "staged_effects": [{
                "id": "start-enc-1", "effect_type": "start_encounter",
                "arguments": {
                    "participants": [
                        {"character_id": str(ctx["owner_pc"])},
                        {"npc_entity_id": str(ctx["goblin_id"])},
                    ],
                    "scene": {"location_name": "Treeline"},
                },
            }],
        })
        staged = [e.model_dump(mode="json") for e in contract.staged_effects]
        apply_staged_effects(db, campaign, staged, turn, attempt)
        db.commit()
        encounter = get_active_encounter(db, ctx["campaign_id"])
        assert encounter is not None
        assert encounter.start_source == "dm_effect"
        assert encounter.participant_count == 2
        assert encounter.source_turn_id == turn.id
        # Duplicate effect replay is a no-op.
        apply_staged_effects(db, campaign, staged, turn, attempt)
        db.commit()
        assert get_active_encounter(db, ctx["campaign_id"]).id == encounter.id
        assert len(list_participants(db, encounter.id)) == 2


def test_dm_effect_turn_commit_binds_start_provenance_and_outbox():
    """Full turn commit with a staged start_encounter binds the turn event as
    start provenance and enqueues the encounter.started projection."""
    from datetime import datetime, timezone

    from app.dm.contract import normalize_contract
    from app.dm.turns import commit_turn_with_effects, mark_streaming_started, stage_validated_attempt
    from models.dm import DmTurn, DmTurnAttempt, DMStream, DMStreamChunk
    from models.reliability import Outbox

    fac, ctx = _fixture()
    with fac() as db:
        turn = db.get(DmTurn, ctx["turn_id"])
        attempt = db.get(DmTurnAttempt, ctx["attempt_id"])
        contract = normalize_contract({
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond",
            "reason": "combat begins",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "Goblins attack!", "claim_kind": "observation",
                            "origin": "dm_adjudication"}],
            }],
            "staged_effects": [{
                "id": "start-enc-1", "effect_type": "start_encounter",
                "arguments": {
                    "participants": [
                        {"character_id": str(ctx["owner_pc"])},
                        {"npc_entity_id": str(ctx["goblin_id"])},
                    ],
                    "scene": {"location_name": "Treeline"},
                },
            }],
        })
        stage_validated_attempt(db, attempt.id, contract)
        stream = DMStream(
            id=uuid.uuid4(), campaign_id=turn.campaign_id,
            thread_id=uuid.UUID(str(turn.thread_id)),
            turn_id=str(turn.id), attempt_id=str(attempt.id),
            status="streaming", audience=turn.audience,
        )
        db.add(stream)
        db.flush()
        text = "Goblins burst from the treeline!"
        db.add(DMStreamChunk(
            id=uuid.uuid4(), stream_id=stream.id, sequence=0,
            text=text, byte_length=len(text.encode()),
        ))
        stream.first_chunk_at = datetime.now(timezone.utc)
        stream.chunk_count = 1
        db.flush()
        mark_streaming_started(db, turn.id, attempt.id, stream.id)
        _, _, event = commit_turn_with_effects(db, turn.id, attempt.id)
        encounter = get_active_encounter(db, ctx["campaign_id"])
        assert encounter is not None
        assert encounter.start_source == "dm_effect"
        # The turn event is the start's provenance: bound, resolvable, surfaced.
        assert encounter.created_event_id == event.id
        view = encounter_view(db, encounter, ctx["owner"], is_owner=True)
        assert view["created_event_id"] == str(event.id)
        assert view["created_event_sequence"] == event.sequence
        started_rows = [
            row for row in db.execute(
                select(Outbox).where(Outbox.campaign_id == ctx["campaign_id"])
            ).scalars().all() if row.event_type == "encounter.started"
        ]
        assert len(started_rows) == 1
        assert started_rows[0].operation_id == f"encounter:{encounter.id}:started"


def test_stale_source_attempt_id_is_rejected_not_rewritten():
    fac, ctx = _fixture()
    with fac() as db:
        # A second submission supersedes the fixture attempt: it is now stale.
        accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["owner"],
            character_id=ctx["owner_pc"], raw_content="I draw my blade!",
            segments=[{"type": "ic", "text": "I draw my blade!"}],
            thread_id=ctx["thread_id"],
        )
        db.commit()
        turn, fresh_attempt = coordinate_turn(db, ctx["campaign_id"], ctx["thread_id"])
        assert fresh_attempt.id != ctx["attempt_id"]
        with pytest.raises(EncounterError, match="current attempt"):
            _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}],
                   operation_id="op-stale-attempt", attempt_id=ctx["attempt_id"])
            db.rollback()
        # The current attempt is accepted.
        encounter, _ = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}],
                              operation_id="op-fresh-attempt", attempt_id=fresh_attempt.id)
        assert encounter.source_attempt_id == fresh_attempt.id


def test_contract_rejects_ambiguous_participant_selection():
    from app.dm.contract import ContractValidationError, normalize_contract

    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond",
            "reason": "bad selection",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "Fight!", "claim_kind": "observation",
                            "origin": "dm_adjudication"}],
            }],
            "staged_effects": [{
                "id": "start-enc-bad", "effect_type": "start_encounter",
                "arguments": {"participants": [{"character_id": "abc", "npc_entity_id": "def"}]},
            }],
        })


# ── #204 interplay: delegation + cancel guard ───────────────────────────────


def test_rolls_service_delegates_encounter_fulfillment_and_rejects_cancel():
    from app.rolls.service import (
        RollAuthorizationError,
        RollLifecycleError,
        cancel_or_replace,
        fulfill_roll,
    )
    from models.profiles import Profile as ProfileModel

    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        outsider = uuid.uuid4()
        db.add(ProfileModel(id=outsider, email="outsider@example.com"))
        db.commit()
        # Encounter authorization failures keep the roll API contract: 403
        # mapping, never a 500 from an untranslated PermissionError.
        with pytest.raises(RollAuthorizationError):
            fulfill_roll(
                db, request_id=owner_p.roll_request_id, actor_id=outsider,
                payload={"source": "app", "raw_rolls": [10],
                         "modifier": owner_p.initiative_modifier,
                         "total": 10 + owner_p.initiative_modifier},
            )
        req, fulfillment, resumed, encounter_ready = fulfill_roll(
            db, request_id=owner_p.roll_request_id, actor_id=ctx["owner"],
            payload={"source": "physical", "raw_rolls": [9],
                     "modifier": owner_p.initiative_modifier,
                     "total": 9 + owner_p.initiative_modifier},
        )
        assert resumed is None  # encounter resumes, never the source DM turn
        assert encounter_ready == {"encounter_id": str(encounter.id), "ready": True}
        assert fulfillment.source == "physical"
        assert db.get(EncounterParticipant, owner_p.id).roll_source == "human_physical"
        with pytest.raises(RollLifecycleError, match="cannot be cancelled"):
            cancel_or_replace(db, request_id=owner_p.roll_request_id, replacement=None)


# ── realtime hooks ──────────────────────────────────────────────────────────


def test_realtime_hooks_emit_stable_encounter_events():
    from app.realtime.service import (
        InMemoryRealtimePublisher,
        build_encounter_ready_event,
        build_encounter_started_event,
        get_realtime_publisher,
        set_realtime_publisher,
    )

    fac, ctx = _fixture()
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        with fac() as db:
            encounter, _ = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
            started = [p for p in recorder.published if p["event"] == "encounter.started"]
            assert len(started) == 1
            assert started[0]["payload"]["encounter_id"] == str(encounter.id)
            assert started[0]["payload"]["dedupe_key"] == f"{encounter.id}:started"
            owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
            _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
            ready = [p for p in recorder.published if p["event"] == "encounter.initiative_ready"]
            assert len(ready) == 1
            assert ready[0]["payload"]["turn_order_ids"] == [str(owner_p.id)]
            # Builders carry no hidden stat breakdowns.
            assert "initiative_modifier" not in str(ready[0]["payload"])
            assert build_encounter_started_event(encounter)["event_id"] == f"encounter:{encounter.id}:started"
            assert build_encounter_ready_event(encounter)["event_id"] == f"encounter:{encounter.id}:ready"
    finally:
        set_realtime_publisher(previous)


# ── HTTP transport: start + idempotent replay ───────────────────────────────


def test_http_start_and_duplicate_replay(monkeypatch):
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        db.add(ProfileModel(id=TEST_USER_ID, email="owner@example.com"))
        db.commit()
    with fac() as db:
        ctx = _seed_world(db, second_pc=False, npc=False)
        # Re-key the seeded world onto the HTTP test user.
        owner, campaign_id = ctx["owner"], ctx["campaign_id"]
        camp = db.get(Campaign, campaign_id)
        camp.owner_id = TEST_USER_ID
        for member in db.execute(
            select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
        ).scalars().all():
            if member.user_id == owner:
                member.user_id = TEST_USER_ID
        for char in db.execute(select(Character)).scalars().all():
            if char.owner_id == owner:
                char.owner_id = TEST_USER_ID
        for sheet in db.execute(select(Dnd5eCharacterSheet)).scalars().all():
            if sheet.owner_id == owner:
                sheet.owner_id = TEST_USER_ID
        for req in db.execute(select(PlayerRollRequest)).scalars().all():
            if req.requested_user_id == owner:
                req.requested_user_id = TEST_USER_ID
        db.commit()
        owner_pc = ctx["owner_pc"]
        turn_id, attempt_id, revision = ctx["turn_id"], ctx["attempt_id"], 0

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, TEST_USER_ID)

    monkeypatch.setattr("app.combat.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        body = {
            "expected_revision": revision,
            "source_turn_id": str(turn_id),
            "source_attempt_id": str(attempt_id),
            "participants": [{"character_id": str(owner_pc)}],
        }
        first = client.post(f"/api/campaigns/{campaign_id}/encounters", json=body,
                            headers={"Idempotency-Key": "http-start-1"})
        assert first.status_code == 201, first.text
        assert first.headers["X-Idempotent-Replay"] == "false"
        encounter_id = first.json()["encounter"]["id"]
        replay = client.post(f"/api/campaigns/{campaign_id}/encounters", json=body,
                             headers={"Idempotency-Key": "http-start-1"})
        assert replay.status_code == 201
        assert replay.headers["X-Idempotent-Replay"] == "true"
        assert replay.json()["encounter"]["id"] == encounter_id
        active = client.get(f"/api/campaigns/{campaign_id}/encounters/active")
        assert active.status_code == 200
        assert active.json()["encounter"]["id"] == encounter_id
        # Turn order is not authoritative while initiative is pending.
        order = client.get(f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-order")
        assert order.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_http_generic_fulfill_emits_ready_event(monkeypatch):
    """Issue #230 round 5: the last initiative fulfilled through the generic
    roll-request API must still publish encounter.initiative_ready post-commit.
    """
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from app.realtime.service import (
        InMemoryRealtimePublisher,
        get_realtime_publisher,
        set_realtime_publisher,
    )
    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        db.add(ProfileModel(id=TEST_USER_ID, email="owner@example.com"))
        db.commit()
    with fac() as db:
        ctx = _seed_world(db, second_pc=False, npc=False)
        owner, campaign_id = ctx["owner"], ctx["campaign_id"]
        camp = db.get(Campaign, campaign_id)
        camp.owner_id = TEST_USER_ID
        for member in db.execute(
            select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
        ).scalars().all():
            if member.user_id == owner:
                member.user_id = TEST_USER_ID
        for char in db.execute(select(Character)).scalars().all():
            if char.owner_id == owner:
                char.owner_id = TEST_USER_ID
        for sheet in db.execute(select(Dnd5eCharacterSheet)).scalars().all():
            if sheet.owner_id == owner:
                sheet.owner_id = TEST_USER_ID
        for req in db.execute(select(PlayerRollRequest)).scalars().all():
            if req.requested_user_id == owner:
                req.requested_user_id = TEST_USER_ID
        db.commit()
        owner_pc = ctx["owner_pc"]
        turn_id, attempt_id = ctx["turn_id"], ctx["attempt_id"]

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, TEST_USER_ID)

    monkeypatch.setattr("app.combat.router.resolve_profile", resolve_test_profile)
    monkeypatch.setattr("app.rolls.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        client = TestClient(app)
        start = client.post(
            f"/api/campaigns/{campaign_id}/encounters",
            json={
                "expected_revision": 0,
                "source_turn_id": str(turn_id),
                "source_attempt_id": str(attempt_id),
                "participants": [{"character_id": str(owner_pc)}],
            },
            headers={"Idempotency-Key": "http-ready-start"},
        )
        assert start.status_code == 201, start.text
        encounter_id = start.json()["encounter"]["id"]
        with fac() as db:
            participant = next(
                p for p in list_participants(db, uuid.UUID(encounter_id))
                if p.character_id == owner_pc
            )
            roll_request_id, modifier = participant.roll_request_id, participant.initiative_modifier
        fulfill = client.post(
            f"/api/campaigns/{campaign_id}/roll-requests/{roll_request_id}/fulfill",
            json={"source": "app", "raw_rolls": [12],
                  "modifier": modifier, "total": 12 + modifier},
            headers={"Idempotency-Key": "http-ready-fulfill"},
        )
        assert fulfill.status_code == 200, fulfill.text
        assert fulfill.json()["encounter_ready"] == {
            "encounter_id": encounter_id, "ready": True,
        }
        ready = [p for p in recorder.published if p["event"] == "encounter.initiative_ready"]
        assert len(ready) == 1
        assert ready[0]["payload"]["encounter_id"] == encounter_id
        assert ready[0]["payload"]["event_id"] == f"encounter:{encounter_id}:ready"
        assert ready[0]["payload"]["dedupe_key"] == f"{encounter_id}:ready"
    finally:
        set_realtime_publisher(previous)
        app.dependency_overrides.clear()


# ── private-thread audience ───────────────────────────────────────────────


def _private_thread_fixture():
    """Owner-only private thread with its own turn/attempt in this campaign."""
    from app.runtime.threads import create_private_thread

    fac, ctx = _fixture()
    with fac() as db:
        thread = create_private_thread(
            db, campaign_id=ctx["campaign_id"], created_by=ctx["owner"],
            member_ids=[ctx["owner"]], title="Side Room",
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
        ctx = dict(ctx, private_thread_id=thread.id, private_turn_id=turn.id,
                   private_attempt_id=attempt.id)
    return fac, ctx


def test_private_thread_encounter_hidden_from_non_members():
    fac, ctx = _private_thread_fixture()
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-private-1", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["private_turn_id"],
            source_attempt_id=ctx["private_attempt_id"],
            scene={"location_name": "Side Room"},
            participants=[{"character_id": str(ctx["owner_pc"])}],
        )
        assert encounter.thread_id == str(ctx["private_thread_id"])
        # Thread member sees it; campaign member outside the thread does not.
        assert can_view_encounter(db, encounter, ctx["owner"]) is True
        assert can_view_encounter(db, encounter, ctx["player"]) is False
        assert get_snapshot_encounter(db, ctx["campaign_id"], ctx["player"]) is None
        visible = get_snapshot_encounter(db, ctx["campaign_id"], ctx["owner"])
        assert visible is not None and visible["id"] == str(encounter.id)


def test_private_thread_rejects_unreadable_controller():
    fac, ctx = _private_thread_fixture()
    with fac() as db:
        # The player controls player_pc but cannot read the private thread:
        # selecting them must fail fast, not strand an invisible request.
        with pytest.raises(EncounterError, match="cannot read the encounter thread"):
            start_encounter(
                db, ctx["campaign_id"], operation_id="op-private-2", expected_revision=0,
                actor_id=ctx["owner"], source_turn_id=ctx["private_turn_id"],
                source_attempt_id=ctx["private_attempt_id"],
                scene={"location_name": "Side Room"},
                participants=[{"character_id": str(ctx["player_pc"])}],
            )
        db.rollback()


def test_http_private_thread_encounter_reads_hidden(monkeypatch):
    from fastapi.testclient import TestClient

    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    fac, ctx = _private_thread_fixture()
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-private-http", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["private_turn_id"],
            source_attempt_id=ctx["private_attempt_id"],
            scene={"location_name": "Side Room"},
            participants=[{"character_id": str(ctx["owner_pc"])}],
        )
        db.commit()
        encounter_id = str(encounter.id)
        campaign_id = str(ctx["campaign_id"])
        owner_id, player_id = str(ctx["owner"]), str(ctx["player"])

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, uuid.UUID(request.headers["x-test-user"]))

    monkeypatch.setattr("app.combat.router.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        member_headers = {"x-test-user": owner_id}
        outsider_headers = {"x-test-user": player_id}
        active_member = client.get(
            f"/api/campaigns/{campaign_id}/encounters/active", headers=member_headers)
        assert active_member.status_code == 200
        assert active_member.json()["encounter"]["id"] == encounter_id
        active_outsider = client.get(
            f"/api/campaigns/{campaign_id}/encounters/active", headers=outsider_headers)
        assert active_outsider.status_code == 200
        assert active_outsider.json()["encounter"] is None
        one_member = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}", headers=member_headers)
        assert one_member.status_code == 200
        one_outsider = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}", headers=outsider_headers)
        assert one_outsider.status_code == 404
        order_outsider = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-order",
            headers=outsider_headers)
        assert order_outsider.status_code == 404
    finally:
        app.dependency_overrides.clear()
