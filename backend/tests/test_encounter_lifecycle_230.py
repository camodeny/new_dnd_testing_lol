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
    find_created_event,
    fulfill_human_initiative,
    get_active_encounter,
    get_active_participant,
    get_snapshot_encounter,
    get_turn_order,
    list_participants,
)
from app.dm.turns import coordinate_turn  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant  # noqa: E402
from models.dm import PlayerRollFulfillment, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402
from tests.support.combat import dm_start_encounter  # noqa: E402


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
        "owner_pc": owner_pc, "player_pc": player_pc, "absent_pc": absent_pc,
        "goblin_id": goblin_id, "turn_id": turn.id, "attempt_id": attempt.id,
    }


def _fixture():
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db)
    return fac, ctx


def _start(db, ctx, participants, *, scene=None, npc_d20=None, effect_id=None,
           turn_id=None, attempt_id=None):
    """Start via the AI DM's staged effect. Returns the Encounter (committed)."""
    return dm_start_encounter(
        db, ctx["campaign_id"], turn_id or ctx["turn_id"],
        attempt_id or ctx["attempt_id"], participants,
        scene=scene or {"location_name": "Treeline"},
        npc_d20=npc_d20, effect_id=effect_id,
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


def test_solo_start_selects_only_listed_pc_and_defers_start_event():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
        assert encounter.status == "pending_initiative"
        assert encounter.round == 1
        assert encounter.revision == 1
        assert encounter.start_source == "dm_effect"
        assert encounter.participant_count == 1
        assert encounter.active_participant_id is None
        # The helper applies the staged effect directly; the encounter.started
        # lifecycle event and campaign revision advance at commit_turn.
        assert find_created_event(db, encounter) is None
        assert db.get(Campaign, ctx["campaign_id"]).revision == 0
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
        encounter = _start(
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
            _start(db, ctx, [{"character_id": str(ctx["player_pc"])}])


# ── human + NPC initiative, ordering, readiness ─────────────────────────────


def test_human_and_npc_initiative_complete_to_active_with_order():
    fac, ctx = _fixture()
    with fac() as db:
        # NPC initiative is rolled by code at start (npc_d20=15 pins the die).
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},   # +4
            {"character_id": str(ctx["player_pc"])},  # +2
            {"npc_entity_id": str(ctx["goblin_id"])},  # +2
        ], npc_d20=15)
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        goblin_p = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        assert goblin_p.roll_request_id is None  # runtime path, no #204 request
        # NPC is already fulfilled at start; encounter stays pending on humans.
        assert encounter.status == "pending_initiative"
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
        # Domain events exist in campaign order. The encounter.started event
        # is staged at commit_turn (not by the direct staged-effect helper),
        # so only readiness + first turn are present here. Issue #231 opens
        # turn 1 atomically with readiness, appending encounter.turn_started.
        from app.combat.service import TURN_STARTED_EVENT  # noqa: E402

        types = [e.event_type for e in list_campaign_events(db, ctx["campaign_id"])]
        assert types == [ENCOUNTER_READY_EVENT, TURN_STARTED_EVENT]


def test_dm_start_fulfills_npcs_immediately_and_last_pc_roll_readies():
    """NPCs are fulfilled with roll_source dm_runtime right after a DM start;
    the encounter becomes active when the last PC rolls."""
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ], npc_d20=10)
        assert encounter.status == "pending_initiative"
        assert encounter.active_participant_id is None
        parts = {p.kind: p for p in list_participants(db, encounter.id)}
        assert parts["npc"].initiative_status == "fulfilled"
        assert parts["npc"].roll_source == "dm_runtime"
        assert parts["npc"].initiative_total == 12  # 10 + 2
        assert parts["npc"].roll_request_id is None
        assert parts["pc"].initiative_status == "pending"
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        _, _, _, encounter, ready_event = _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
        assert ready_event is not None
        assert db.get(Encounter, encounter.id).status == "active"
        assert get_active_participant(db, encounter.id).id == owner_p.id


def test_npcs_fulfilled_at_dm_start_and_human_path_refuses_npc():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ], npc_d20=15)
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        goblin_p = db.execute(
            select(EncounterParticipant).where(EncounterParticipant.encounter_id == encounter.id,
                                               EncounterParticipant.kind == "npc")
        ).scalars().one()
        assert goblin_p.initiative_status == "fulfilled"
        assert goblin_p.roll_source == "dm_runtime"
        with pytest.raises(EncounterError, match="runtime roll path"):
            fulfill_human_initiative(
                db, encounter.id, goblin_p.id, actor_id=ctx["owner"],
                payload={"source": "app", "raw_rolls": [10], "modifier": 2, "total": 12},
            )
        assert owner_p.initiative_status == "pending"

    fac2, ctx2 = _fixture()
    with fac2() as db:
        # Out-of-range NPC dice are rejected by the same bounded parser.
        with pytest.raises(EncounterError, match="between 1 and 20"):
            _start(db, ctx2, [
                {"npc_entity_id": str(ctx2["goblin_id"])},
            ], npc_d20=99)


def test_tie_handling_is_deterministic_2024():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [
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
        encounter = _start(db, ctx2, [{"character_id": str(ctx2["owner_pc"])}])
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
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ], npc_d20=15)
        goblin_p = db.execute(
            select(EncounterParticipant).where(EncounterParticipant.encounter_id == encounter.id,
                                               EncounterParticipant.kind == "npc")
        ).scalars().one()
        # NPC was fulfilled by code at start; the human roll is still pending.
        assert goblin_p.initiative_total == 17
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


def test_ready_operation_key_bounded():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        _, _, _, encounter, event = _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
        assert encounter.status == "active"
        assert event is not None
        assert event.operation_id == f"encounter:{encounter.id}:initiative-ready"
        assert len(event.operation_id) <= 128
        event_keys = {
            row.operation_id for row in db.execute(
                select(CampaignDomainEvent).where(
                    CampaignDomainEvent.campaign_id == ctx["campaign_id"])
            ).scalars().all()
        }
        assert all(len(key or "") <= 128 for key in event_keys)


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
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["player_pc"])},
            {"npc_entity_id": str(brute.id), "initiative_modifier": 5},
        ], npc_d20=9)
        npc_p = next(p for p in list_participants(db, encounter.id) if p.kind == "monster")
        assert npc_p.initiative_modifier == 5
        assert npc_p.dex_modifier == 3
        assert npc_p.initiative_status == "fulfilled"
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        _, _, _, encounter, _ = _fulfill(db, encounter.id, player_p, ctx["player"], 12)
        order = get_turn_order(db, encounter.id)
        assert [p.id for p in order] == [npc_p.id, player_p.id]


# ── fulfillment authorization / single-application ──────────────────────────

def test_fulfill_authorization_and_single_application():
    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"character_id": str(ctx["player_pc"])},
        ])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        # Wrong human cannot roll for another PC.
        with pytest.raises(EncounterAuthorizationError):
            _fulfill(db, encounter.id, owner_p, ctx["player"], 10)
        # Arithmetic is code-owned: lying totals/modifiers rejected.
        with pytest.raises(EncounterError, match="must equal d20"):
            fulfill_human_initiative(
                db, encounter.id, owner_p.id, actor_id=ctx["owner"],
                payload={"source": "app", "raw_rolls": [10],
                         "modifier": owner_p.initiative_modifier, "total": 999},
            )
        _fulfill(db, encounter.id, owner_p, ctx["owner"], 10)
        # Duplicate fulfillment of the same request cannot apply twice.
        with pytest.raises(EncounterError, match="cannot be fulfilled"):
            _fulfill(db, encounter.id, owner_p, ctx["owner"], 10)
        assert len(db.execute(select(PlayerRollFulfillment)).scalars().all()) == 1
        # Retry preserves already submitted initiative: PC1 still fulfilled.
        assert db.get(EncounterParticipant, owner_p.id).initiative_status == "fulfilled"
        _fulfill(db, encounter.id, player_p, ctx["player"], 12)
        assert db.get(Encounter, encounter.id).status == "active"


def test_off_roster_and_terminal_pcs_are_rejected():
    from models.campaigns import CampaignPcLifecycle

    fac, ctx = _fixture()
    with fac() as db:
        # absent_pc belongs to a member but is not selected: off-roster.
        with pytest.raises(EncounterError, match="active roster"):
            _start(db, ctx, [{"character_id": str(ctx["absent_pc"])}])
        db.rollback()
        # A selected PC with a terminal lifecycle cannot join combat.
        db.add(CampaignPcLifecycle(
            campaign_id=ctx["campaign_id"], character_id=ctx["player_pc"],
            user_id=ctx["player"], status="dead",
        ))
        db.commit()
        with pytest.raises(EncounterError, match="is dead"):
            _start(db, ctx, [{"character_id": str(ctx["player_pc"])}])
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
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ], npc_d20=15)
        encounter_id = encounter.id
        goblin_id = next(p.id for p in list_participants(db, encounter_id) if p.kind == "npc")
        assert db.get(EncounterParticipant, goblin_id).initiative_total == 17
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
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["player_pc"])},
            {"npc_entity_id": str(ctx["goblin_id"])},
        ], npc_d20=15)
        owner_view = encounter_view(db, encounter, ctx["owner"])
        member_view = encounter_view(db, encounter, ctx["player"])
        npc_owner = next(p for p in owner_view["participants"] if p["kind"] == "npc")
        npc_member = next(p for p in member_view["participants"] if p["kind"] == "npc")
        # The AI is the only DM: the owner is a player and gets no NPC stats.
        assert npc_owner == npc_member
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


# ── #204 interplay: delegation ─────────────────────────────────────────────


def test_rolls_service_delegates_encounter_fulfillment():
    from app.rolls.service import (
        RollAuthorizationError,
        fulfill_roll,
    )
    from models.profiles import Profile as ProfileModel

    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
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


# ── realtime hooks ──────────────────────────────────────────────────────────

def test_realtime_hooks_emit_stable_encounter_events():
    from app.realtime.service import (
        build_encounter_ready_event,
        build_encounter_started_event,
        get_realtime_publisher,
        set_realtime_publisher,
    )
    from tests.support.realtime import InMemoryRealtimePublisher

    fac, ctx = _fixture()
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        with fac() as db:
            # The direct staged-effect helper runs outside commit_turn, so no
            # encounter.started lifecycle event or realtime publish happens
            # here; readiness (below) still publishes post-commit.
            encounter = _start(db, ctx, [{"character_id": str(ctx["owner_pc"])}])
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


# ── HTTP transport: generic fulfill still readies ───────────────────────────


def test_http_generic_fulfill_emits_ready_event(monkeypatch):
    """Issue #230 round 5: the last initiative fulfilled through the generic
    roll-request API must still publish encounter.initiative_ready post-commit.
    """
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from app.realtime.service import get_realtime_publisher, set_realtime_publisher
    from tests.support.realtime import InMemoryRealtimePublisher
    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    eng = _engine()
    # Mirror PostgreSQL's append-only event constraint. Completing initiative
    # must insert readiness/turn events without updating either afterward.
    with eng.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_campaign_domain_event_mutation
            BEFORE UPDATE ON campaign_domain_events
            BEGIN
                SELECT RAISE(ABORT, 'campaign domain events are immutable');
            END
        """)
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

    monkeypatch.setattr("app.deps.auth.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    previous = get_realtime_publisher()
    recorder = InMemoryRealtimePublisher()
    set_realtime_publisher(recorder)
    try:
        # The encounter starts via the AI DM effect (POST /encounters is gone).
        with fac() as db:
            started = dm_start_encounter(
                db, campaign_id, turn_id, attempt_id,
                [{"character_id": str(owner_pc)}],
                scene={"location_name": "Treeline"},
            )
            participant = next(
                p for p in list_participants(db, started.id)
                if p.character_id == owner_pc
            )
            roll_request_id, modifier = participant.roll_request_id, participant.initiative_modifier
            encounter_id = str(started.id)
        client = TestClient(app)
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


# ── private-thread audience ─────────────────────────────────────────────────


def _private_thread_fixture():
    """Owner-only private thread with its own turn/attempt in this campaign."""
    from app.threads.service import create_private_thread

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
        encounter = dm_start_encounter(
            db, ctx["campaign_id"], ctx["private_turn_id"], ctx["private_attempt_id"],
            [{"character_id": str(ctx["owner_pc"])}],
            scene={"location_name": "Side Room"},
        )
        assert encounter.thread_id == str(ctx["private_thread_id"])
        # Thread member sees it; campaign member outside the thread does not.
        assert can_view_encounter(db, encounter, ctx["owner"]) is True
        assert can_view_encounter(db, encounter, ctx["player"]) is False
        assert get_snapshot_encounter(db, ctx["campaign_id"], ctx["player"]) is None
        visible = get_snapshot_encounter(db, ctx["campaign_id"], ctx["owner"])
        assert visible is not None and visible["id"] == str(encounter.id)
        # Activating writes the ready + first-turn events; no lifecycle or
        # turn event may reach the non-thread member's campaign history feed.
        # (encounter.started itself is staged at commit_turn, so the direct
        # helper path only produces readiness + first turn here.)
        from app.combat.service import TURN_STARTED_EVENT  # noqa: E402

        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
        feed_outsider = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["player"])
        assert all(
            e.event_type not in (ENCOUNTER_STARTED_EVENT, ENCOUNTER_READY_EVENT, TURN_STARTED_EVENT)
            for e in feed_outsider
        )
        feed_member = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["owner"])
        assert {ENCOUNTER_READY_EVENT} <= {e.event_type for e in feed_member}
        # Unscoped callers (provenance/audit) still see full history.
        assert {ENCOUNTER_READY_EVENT} <= {
            e.event_type for e in list_campaign_events(db, ctx["campaign_id"])
        }


def test_private_thread_rejects_unreadable_controller():
    fac, ctx = _private_thread_fixture()
    with fac() as db:
        # The player controls player_pc but cannot read the private thread:
        # selecting them must fail fast, not strand an invisible request.
        with pytest.raises(EncounterError, match="cannot read the encounter thread"):
            dm_start_encounter(
                db, ctx["campaign_id"], ctx["private_turn_id"], ctx["private_attempt_id"],
                [{"character_id": str(ctx["player_pc"])}],
                scene={"location_name": "Side Room"},
            )
        db.rollback()


def test_http_private_thread_encounter_reads_hidden(monkeypatch):
    from fastapi.testclient import TestClient

    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    fac, ctx = _private_thread_fixture()
    with fac() as db:
        encounter = dm_start_encounter(
            db, ctx["campaign_id"], ctx["private_turn_id"], ctx["private_attempt_id"],
            [{"character_id": str(ctx["owner_pc"])}],
            scene={"location_name": "Side Room"},
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

    monkeypatch.setattr("app.deps.auth.resolve_profile", resolve_test_profile)
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
        # Lifecycle history follows the same thread boundary.
        events_outsider = client.get(
            f"/api/campaigns/{campaign_id}/events", headers=outsider_headers)
        assert events_outsider.status_code == 200
        assert all(
            e["event_type"] not in ("encounter.started", "encounter.initiative_ready")
            for e in events_outsider.json()["events"]
        )
        # Lifecycle history follows the same thread boundary. The direct
        # staged-effect helper stages no encounter.started event (that
        # happens at commit_turn), so neither feed has lifecycle events yet.
        events_member = client.get(
            f"/api/campaigns/{campaign_id}/events", headers=member_headers)
        assert events_member.status_code == 200
        assert all(
            e["event_type"] not in ("encounter.started", "encounter.initiative_ready")
            for e in events_member.json()["events"]
        )
    finally:
        app.dependency_overrides.clear()


def test_turn_order_redacts_other_controllers_roll_requests(monkeypatch):
    """Turn-order matches encounter_view: another PC's roll_request_id stays
    with its controller — the campaign owner included."""
    from fastapi.testclient import TestClient

    from database import get_db
    from main import app
    from models.profiles import Profile as ProfileModel

    fac, ctx = _fixture()
    with fac() as db:
        encounter = _start(db, ctx, [
            {"character_id": str(ctx["owner_pc"])},
            {"character_id": str(ctx["player_pc"])},
        ])
        owner_p = _pc_participant(db, encounter.id, ctx["owner_pc"])
        player_p = _pc_participant(db, encounter.id, ctx["player_pc"])
        _fulfill(db, encounter.id, owner_p, ctx["owner"], 12)
        _fulfill(db, encounter.id, player_p, ctx["player"], 9)
        db.commit()
        encounter_id = str(encounter.id)
        campaign_id = str(ctx["campaign_id"])
        owner_id, player_id = str(ctx["owner"]), str(ctx["player"])

    def override_db():
        with fac() as db:
            yield db

    def resolve_test_profile(request, db):
        return db.get(ProfileModel, uuid.UUID(request.headers["x-test-user"]))

    monkeypatch.setattr("app.deps.auth.resolve_profile", resolve_test_profile)
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        as_player = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-order",
            headers={"x-test-user": player_id},
        )
        assert as_player.status_code == 200, as_player.text
        by_controller = {
            p["controller_user_id"]: p for p in as_player.json()["order"]
        }
        assert by_controller[player_id]["roll_request_id"]
        assert "roll_request_id" not in by_controller[owner_id]
        as_owner = client.get(
            f"/api/campaigns/{campaign_id}/encounters/{encounter_id}/turn-order",
            headers={"x-test-user": owner_id},
        )
        assert as_owner.status_code == 200, as_owner.text
        owner_by_controller = {
            p["controller_user_id"]: p for p in as_owner.json()["order"]
        }
        assert owner_by_controller[owner_id]["roll_request_id"]
        assert "roll_request_id" not in owner_by_controller[player_id]
    finally:
        app.dependency_overrides.clear()


def test_dm_start_from_unreadable_thread_is_rejected():
    """A DM start whose participant controller cannot read the source thread
    fails fast instead of stranding an invisible initiative request."""
    fac, ctx = _fixture()
    with fac() as db:
        from app.threads.service import create_private_thread

        thread = create_private_thread(
            db, campaign_id=ctx["campaign_id"], created_by=ctx["player"],
            member_ids=[ctx["player"]], title="Whispers",
        )
        db.commit()
        accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["player"],
            character_id=ctx["player_pc"],
            raw_content="Something moves in the dark!",
            segments=[{"type": "ic", "text": "Something moves in the dark!"}],
            thread_id=str(thread.id),
        )
        db.commit()
        turn, attempt = coordinate_turn(db, ctx["campaign_id"], str(thread.id))
        db.commit()
        # The owner_pc's controller (the owner) cannot read this private thread.
        with pytest.raises(EncounterError, match="cannot read the encounter thread"):
            dm_start_encounter(
                db, ctx["campaign_id"], turn.id, attempt.id,
                [{"character_id": str(ctx["owner_pc"])}],
            )
        db.rollback()
        assert get_active_encounter(db, ctx["campaign_id"]) is None


def test_private_attempt_promotes_start_encounter():
    """A staged start_encounter commits from a private DM attempt; the
    encounter stays thread-scoped for non-members."""
    from datetime import datetime, timezone

    from app.dm.contract import normalize_contract
    from app.dm.turns import commit_turn, mark_streaming_started, stage_validated_attempt
    from models.dm import DMStream, DMStreamChunk
    from app.threads.service import create_private_thread

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
        attempt.audience = "private"
        turn.audience = "private"
        db.commit()
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
                    "participants": [{"character_id": str(ctx["owner_pc"])}],
                    "scene": {"location_name": "Side Room"},
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
        # Previously rejected: public-defaulted effect broadening a private attempt.
        commit_turn(db, turn.id, attempt.id)
        encounter = get_active_encounter(db, ctx["campaign_id"])
        assert encounter is not None
        assert encounter.thread_id == str(thread.id)
        assert get_snapshot_encounter(db, ctx["campaign_id"], ctx["player"]) is None
        # The structured start stages a distinct thread-scoped lifecycle
        # event in the same outer turn transaction: the thread member sees
        # it in history, the outsider does not.
        member_feed = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["owner"])
        member_started = [e for e in member_feed if e.event_type == ENCOUNTER_STARTED_EVENT]
        assert len(member_started) == 1
        assert member_started[0].payload["encounter_id"] == str(encounter.id)
        assert member_started[0].payload["thread_id"] == str(thread.id)
        assert encounter.created_event_id == member_started[0].id
        outsider_feed = list_campaign_events(db, ctx["campaign_id"], viewer_id=ctx["player"])
        assert all(
            e.event_type != ENCOUNTER_STARTED_EVENT for e in outsider_feed
        )
