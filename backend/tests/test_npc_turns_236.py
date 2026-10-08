"""Issue #236 — the AI DM runs NPC turns; code owns the action economy."""
import uuid
from unittest import mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.combat.npc_turns import NPC_TURN_SOURCE, coordinate_npc_turn  # noqa: E402
from app.combat.service import fulfill_human_initiative  # noqa: E402
from app.combat.turns import (  # noqa: E402
    TurnAuthorizationError,
    TurnError,
    dm_consume_resource,
    dm_end_npc_turn,
    end_turn,
    get_turn_state_row,
)
from app.dm.context import assemble_attempt_context  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.mechanics import npc_turn_issues, resolve_mechanics  # noqa: E402
from app.rules.bestiary import get_stat_block, stat_block_details  # noqa: E402
from app.snapshot.service import _fetch_history  # noqa: E402
from app.submissions.service import list_submissions  # noqa: E402
from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread, PlayerSubmission  # noqa: E402
from models.world import WorldEntity  # noqa: E402
from tests.support.combat import dm_place_tokens, dm_start_encounter  # noqa: E402


class FixedDice:
    """Every die shows ``value`` (capped at its size): pins the NPC's rolls."""

    def __init__(self, value):
        self.value = value

    def randint(self, low, high):
        return max(low, min(high, self.value))


def pinned_dice(value):
    return mock.patch("app.dm.mechanics.random.Random", lambda seed: FixedDice(value))


@pytest.fixture
def table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'npc.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner, camp_id, thread_id, char_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as s:
        s.add_all([
            Profile(id=owner, email="owner@example.com"),
            Campaign(id=camp_id, owner_id=owner, name="Table", revision=0),
            CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner),
            CampaignMember(campaign_id=camp_id, user_id=owner, role="owner", selected_character_id=char_id),
        ])
        s.flush()
        s.add_all([
            Character(id=char_id, owner_id=owner, name="Hero", system="dnd5e"),
            Dnd5eCharacterSheet(character_id=char_id, owner_id=owner, character_name="Hero", level=3,
                                dexterity=14, hit_points_max=20, hit_points_current=20),
        ])
        goblin = WorldEntity(campaign_id=camp_id, entity_type="npc", name="Goblin", visibility="campaign",
                             details=stat_block_details(get_stat_block("goblin-warrior")))
        s.add(goblin)
        s.commit()
        yield {"s": s, "owner": owner, "camp_id": camp_id, "thread_id": thread_id, "char_id": char_id,
               "goblin": goblin}


def _submit(t, text="Steel rings out."):
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    s = t["s"]
    accept_submission(s, campaign_id=t["camp_id"], user_id=t["owner"], character_id=t["char_id"],
                      raw_content=text, segments=[{"type": "ic", "text": text}], thread_id=str(t["thread_id"]))
    s.commit()
    turn, attempt = coordinate_turn(s, t["camp_id"], str(t["thread_id"]), commit=False)
    s.commit()
    return turn, attempt


def _participants(t):
    s = t["s"]
    pc = s.execute(select(EncounterParticipant).where(EncounterParticipant.character_id == t["char_id"])).scalars().one()
    goblin = s.execute(select(EncounterParticipant).where(EncounterParticipant.npc_entity_id == t["goblin"].id)).scalars().one()
    return pc, goblin


def _encounter(t, *, npc_first=True, cells=None):
    """Goblin vs Hero; the goblin wins initiative unless ``npc_first`` is False."""
    s = t["s"]
    turn, attempt = _submit(t)
    encounter = dm_start_encounter(
        s, t["camp_id"], turn.id, attempt.id,
        [{"character_id": str(t["char_id"])}, {"npc_entity_id": str(t["goblin"].id)}],
        map={"width": 40, "height": 40} if cells else None, npc_d20=20 if npc_first else 1,
    )
    pc, goblin = _participants(t)
    raw = 1 if npc_first else 20
    fulfill_human_initiative(s, encounter.id, pc.id, actor_id=t["owner"], payload={
        "source": "app", "raw_rolls": [raw], "modifier": pc.initiative_modifier, "total": raw + pc.initiative_modifier,
    })
    s.commit()
    if cells:
        dm_place_tokens(s, t["camp_id"], turn.id, attempt.id, encounter.id, [
            {"participant_id": str(pc.id), "col": cells[0][0], "row": cells[0][1]},
            {"participant_id": str(goblin.id), "col": cells[1][0], "row": cells[1][1]},
        ])
    s.refresh(encounter)
    return encounter


def _cues(t):
    return t["s"].execute(
        select(PlayerSubmission).where(PlayerSubmission.source == NPC_TURN_SOURCE).order_by(PlayerSubmission.sequence)
    ).scalars().all()


def _attack(t, mech_id="m1", attack="Scimitar", **extra):
    return {
        "id": mech_id, "kind": "attack", "attacker": {"type": "npc", "id": str(t["goblin"].id)},
        "target": {"type": "character", "id": str(t["char_id"])}, "source": attack, **extra,
    }


def _contract(*, mechanics=(), effects=()):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "npc turn",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "The goblin snarls and lunges.", "claim_kind": "observation",
            "origin": "dm_adjudication", "visibility": "public",
        }]}],
        "mechanics": list(mechanics),
        "staged_effects": list(effects),
    })


def _end_turn_effect(encounter, participant, effect_id="end_1"):
    return {"id": effect_id, "effect_type": "npc_end_turn", "arguments": {
        "encounter_id": str(encounter.id), "participant_id": str(participant.id)}}


def _consume_effect(encounter, participant, resource, effect_id="spend_1", **extra):
    return {"id": effect_id, "effect_type": "consume_turn_resource", "arguments": {
        "encounter_id": str(encounter.id), "participant_id": str(participant.id), "resource": resource, **extra}}


def _hp(t) -> int:
    t["s"].expire_all()
    return t["s"].execute(select(Dnd5eCharacterSheet)).scalars().one().hit_points_current


def _cue_attempt(t):
    attempt_id = coordinate_npc_turn(t["s"], t["camp_id"], str(t["thread_id"]))
    assert attempt_id is not None
    attempt = t["s"].get(DmTurnAttempt, uuid.UUID(attempt_id))
    return t["s"].get(DmTurn, attempt.turn_id), attempt


def test_npc_turns_run_through_the_dm_with_no_human_input(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    pc, goblin = _participants(t)
    assert encounter.active_participant_id == goblin.id
    (cue,) = _cues(t)
    assert cue.character_id is None and "Goblin's turn" in cue.raw_content

    turn, attempt = _cue_attempt(t)
    contract = _contract(mechanics=[_attack(t)], effects=[_end_turn_effect(encounter, goblin)])
    with pinned_dice(15):  # d20 15 + 4 = 19 hits AC 12; 1d6+2 = 8
        execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: contract, narrator="deterministic")

    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "succeeded"
    assert _hp(t) == 12
    encounter = s.get(Encounter, encounter.id)
    assert (encounter.active_participant_id, encounter.turn_sequence, encounter.round) == (pc.id, 2, 1)
    assert get_turn_state_row(s, encounter.id, goblin.id).action_available is False
    outcome = s.get(DmTurnAttempt, attempt.id).contract_snapshot["beats"][-1]["claims"][0]["text"]
    assert outcome == "Goblin attacks Hero with Scimitar: hit. Hero takes 8 slashing damage."
    cue = s.get(PlayerSubmission, cue.id)
    assert cue.resolution_status == "resolved"

    # The player ends their turn: round 2 opens with the goblin, queued for the DM.
    end_turn(s, encounter.id, actor_id=t["owner"], expected_turn_sequence=2,
             expected_revision=s.get(Campaign, t["camp_id"]).revision)
    s.expire_all()
    encounter = s.get(Encounter, encounter.id)
    assert (encounter.active_participant_id, encounter.round) == (goblin.id, 2)
    assert get_turn_state_row(s, encounter.id, goblin.id).action_available is True
    assert len(_cues(t)) == 2
    assert coordinate_npc_turn(s, t["camp_id"], str(t["thread_id"])) is not None


def test_npc_turn_cue_is_never_shown_to_players(table):
    t, s = table, table["s"]
    _encounter(t)
    (cue,) = _cues(t)
    assert str(cue.id) not in {m["id"] for m in list_submissions(s, t["camp_id"], str(t["thread_id"]))}
    messages, _, _ = _fetch_history(s, t["camp_id"], str(t["thread_id"]), limit=50, cursor=None)
    assert str(cue.id) not in {m["id"] for m in messages}


def test_only_one_cue_per_npc_turn(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    from app.combat.npc_turns import queue_npc_turn

    assert queue_npc_turn(s, s.get(Campaign, t["camp_id"]), encounter) is None
    assert len(_cues(t)) == 1


def test_player_first_queues_no_cue(table):
    t = table
    _encounter(t, npc_first=False)
    assert _cues(t) == []


def test_context_shows_whose_turn_and_npc_attacks(table):
    t, s = table, table["s"]
    _encounter(t)
    _, attempt = _cue_attempt(t)
    packet = assemble_attempt_context(s, attempt.id)
    (record,) = [r for lane in packet.lanes for r in lane.records if r.record_id.startswith("active-encounter:")]
    assert (record.visibility, record.use) == ("dm_only", "adjudication_only")
    value = record.value["active_encounter"]
    assert value["active_is_npc"] is True
    assert value["active_participant"]["name"] == "Goblin"
    assert value["active_turn_resources"]["action"] is True
    goblin = next(p for p in value["turn_order"] if p["kind"] != "pc")
    assert [a["name"] for a in goblin["attacks"]] == ["Scimitar", "Shortbow"]
    assert goblin["multiattack"] == 1


def _issues(t, contract):
    s = t["s"]
    turn = s.execute(select(DmTurn).order_by(DmTurn.created_at.desc())).scalars().first()
    campaign = s.get(Campaign, t["camp_id"])
    return resolve_mechanics(s, campaign, turn, contract).issues + npc_turn_issues(s, campaign, turn, contract)


def test_one_action_per_turn_and_multiattack_count(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    _, goblin = _participants(t)
    two = _contract(mechanics=[_attack(t, "m1"), _attack(t, "m2")])
    with pinned_dice(15):
        assert [i.code for i in _issues(t, two)] == ["attacks_exhausted"]
        double = _contract(mechanics=[_attack(t)], effects=[_consume_effect(encounter, goblin, "action")])
        assert [i.code for i in _issues(t, double)] == ["action_spent_by_attack"]

        entity = s.get(WorldEntity, t["goblin"].id)
        entity.details = {**entity.details, "multiattack": 2}
        s.commit()
        assert _issues(t, two) == []
        three = _contract(mechanics=[_attack(t, "m1"), _attack(t, "m2"), _attack(t, "m3")])
        assert [i.code for i in _issues(t, three)] == ["attacks_exhausted"]

        # Once the action is spent, a later contract cannot attack again.
        dm_consume_resource(s, encounter, goblin.id, resource="action")
        s.commit()
        assert [i.code for i in _issues(t, _contract(mechanics=[_attack(t)]))] == ["action_spent"]
    with pytest.raises(TurnError, match="action already consumed"):
        dm_consume_resource(s, encounter, goblin.id, resource="action")


def test_dm_never_acts_for_a_player_character(table):
    t, s = table, table["s"]
    encounter = _encounter(t, npc_first=False)
    pc, goblin = _participants(t)
    codes = [i.code for i in _issues(t, _contract(effects=[_end_turn_effect(encounter, pc)]))]
    assert codes == ["player_character"]
    codes = [i.code for i in _issues(t, _contract(effects=[_consume_effect(encounter, pc, "bonus_action")]))]
    assert codes == ["player_character"]
    with pytest.raises(TurnAuthorizationError):
        dm_end_npc_turn(s, encounter, pc.id)
    with pytest.raises(TurnAuthorizationError):
        dm_consume_resource(s, encounter, pc.id, resource="action")
    with pytest.raises(TurnError, match="not Goblin's turn"):
        dm_end_npc_turn(s, encounter, goblin.id)


def test_npc_acts_only_on_its_own_turn(table):
    t = table
    encounter = _encounter(t, npc_first=False)
    _, goblin = _participants(t)
    with pinned_dice(15):
        codes = [i.code for i in _issues(t, _contract(mechanics=[_attack(t)], effects=[_end_turn_effect(encounter, goblin)]))]
    assert codes == ["not_attackers_turn", "not_npcs_turn"]
    # Reactions are the rule-permitted off-turn exception.
    assert _issues(t, _contract(effects=[_consume_effect(encounter, goblin, "reaction")])) == []


def test_npc_attack_checks_reach_and_range(table):
    t = table
    _encounter(t, cells=((0, 0), (20, 0)))  # 100 ft apart
    with pinned_dice(15):
        (issue,) = _issues(t, _contract(mechanics=[_attack(t)]))
        assert issue.code == "out_of_range" and "100 ft away" in issue.message
        # Shortbow 80/320: long range imposes disadvantage (two d20s, lower kept).
        assert _issues(t, _contract(mechanics=[_attack(t, attack="Shortbow")])) == []


def test_unknown_attack_lists_the_stat_block(table):
    t = table
    _encounter(t)
    with pinned_dice(15):
        (issue,) = _issues(t, _contract(mechanics=[_attack(t, attack="Fireball")]))
    assert issue.code == "unknown_attack" and "Scimitar, Shortbow" in issue.message


def test_npc_misses_deal_no_damage(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    _, goblin = _participants(t)
    _, attempt = _cue_attempt(t)
    contract = _contract(mechanics=[_attack(t)], effects=[_end_turn_effect(encounter, goblin)])
    with pinned_dice(1):  # natural 1 always misses
        execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: contract, narrator="deterministic")
    assert _hp(t) == 20
    outcome = s.get(DmTurnAttempt, attempt.id).contract_snapshot["beats"][-1]["claims"][0]["text"]
    assert outcome == "Goblin attacks Hero with Scimitar: miss."


def test_cue_only_turn_sees_the_party_and_hits_them(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    _, goblin = _participants(t)
    # The table's earlier turn already resolved: the NPC's turn holds only the cue.
    for turn in s.execute(select(DmTurn)).scalars().all():
        turn.status = "succeeded"
    for sub in s.execute(select(PlayerSubmission).where(PlayerSubmission.source.is_(None))).scalars().all():
        sub.resolution_status = "resolved"
    s.commit()
    turn, attempt = _cue_attempt(t)
    assert [str(c.id) for c in _cues(t)] == list(turn.submission_ids)
    packet = assemble_attempt_context(s, attempt.id)
    ids = {r.record_id for lane in packet.lanes for r in lane.records}
    assert {f"pc-control:{t['char_id']}", f"character-state:{t['char_id']}"} <= ids

    contract = _contract(mechanics=[_attack(t)], effects=[_end_turn_effect(encounter, goblin)])
    with pinned_dice(15):
        execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: contract, narrator="deterministic")
    s.expire_all()
    assert s.get(DmTurn, turn.id).status == "succeeded"
    assert _hp(t) == 12


def test_hidden_attacker_stays_unnamed_in_the_outcome(table):
    t, s = table, table["s"]
    entity = s.get(WorldEntity, t["goblin"].id)
    entity.visibility = "dm_only"
    s.commit()
    _encounter(t)
    with pinned_dice(15):
        resolution = resolve_mechanics(s, s.get(Campaign, t["camp_id"]), s.execute(select(DmTurn)).scalars().first(),
                                       _contract(mechanics=[_attack(t)]))
    assert resolution.issues == []
    assert resolution.outcomes[0].text == "An unseen creature attacks Hero with Scimitar: hit. Hero takes 8 slashing damage."


def test_cue_never_reaches_narration(table):
    t, s = table, table["s"]
    _encounter(t)
    _, attempt = _cue_attempt(t)
    packet = assemble_attempt_context(s, attempt.id)
    (cue_record,) = [r for lane in packet.lanes for r in lane.records
                     if r.record_id == f"submission:{_cues(t)[0].id}"]
    assert cue_record.use == "adjudication_only"


def test_one_npc_end_turn_per_commit(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    pc, goblin = _participants(t)
    from app.dm.effects import apply_staged_effects

    turn = s.execute(select(DmTurn)).scalars().first()
    effects = [_end_turn_effect(encounter, goblin, "e1"), _end_turn_effect(encounter, pc, "e2")]
    with pytest.raises(ValueError, match="one npc_end_turn"):
        apply_staged_effects(s, s.get(Campaign, t["camp_id"]), effects, turn, s.get(DmTurnAttempt, turn.current_attempt_id))


def test_twin_npcs_spend_their_own_actions(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    encounter = dm_start_encounter(
        s, t["camp_id"], turn.id, attempt.id,
        [{"character_id": str(t["char_id"])}, {"npc_entity_id": str(t["goblin"].id)},
         {"npc_entity_id": str(t["goblin"].id)}], npc_d20=20,
    )
    pc = s.execute(select(EncounterParticipant).where(EncounterParticipant.character_id == t["char_id"])).scalars().one()
    fulfill_human_initiative(s, encounter.id, pc.id, actor_id=t["owner"], payload={
        "source": "app", "raw_rolls": [1], "modifier": pc.initiative_modifier, "total": 1 + pc.initiative_modifier})
    s.commit()
    s.refresh(encounter)
    dm_end_npc_turn(s, encounter, encounter.active_participant_id)  # first twin done
    s.commit()
    second = encounter.active_participant_id
    with pinned_dice(15):
        resolution = resolve_mechanics(s, s.get(Campaign, t["camp_id"]), turn, _contract(mechanics=[_attack(t)]))
    (spend,) = [e for e in resolution.effects if e["effect_type"] == "consume_turn_resource"]
    assert spend["arguments"]["participant_id"] == str(second)


def test_open_npc_turn_gets_one_reminder_then_code_ends_it(table):
    t, s = table, table["s"]
    encounter = _encounter(t)
    pc, goblin = _participants(t)
    forgetful = _contract(mechanics=[_attack(t)])  # attacks but never stages npc_end_turn
    _, attempt = _cue_attempt(t)
    with pinned_dice(15):
        execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: forgetful, narrator="deterministic")
    s.expire_all()
    assert s.get(Encounter, encounter.id).active_participant_id == goblin.id
    cues = _cues(t)
    assert len(cues) == 2 and "still open" in cues[1].raw_content

    _, attempt = _cue_attempt(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _contract(), narrator="deterministic")
    s.expire_all()
    encounter = s.get(Encounter, encounter.id)
    assert (encounter.active_participant_id, encounter.turn_sequence) == (pc.id, 2)
    assert _hp(t) == 12  # the one attack applied once
    assert coordinate_npc_turn(s, t["camp_id"], str(t["thread_id"])) is None


def test_npc_turn_waits_out_a_capacity_pause(table):
    t, s = table, table["s"]
    from app.billing.resolution_guarantee import CapacityPausedError
    from app.combat.npc_turns import queue_missing_npc_turns

    paused = mock.patch(
        "app.billing.resolution_guarantee.require_new_ai_work",
        side_effect=CapacityPausedError("paused", decision={}),
    )
    with paused:
        _encounter(t)
    assert _cues(t) == []
    assert queue_missing_npc_turns(s) != []
    assert len(_cues(t)) == 1
    assert queue_missing_npc_turns(s) == []


def test_sweep_sends_a_reminder_a_paused_commit_skipped(table):
    t, s = table, table["s"]
    from app.billing.resolution_guarantee import CapacityPausedError
    from app.combat.npc_turns import queue_missing_npc_turns

    _encounter(t)
    _, attempt = _cue_attempt(t)
    with mock.patch("app.billing.resolution_guarantee.require_new_ai_work",
                    side_effect=CapacityPausedError("paused", decision={})):
        execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _contract(), narrator="deterministic")
    assert len(_cues(t)) == 1
    assert queue_missing_npc_turns(s) != []
    assert "still open" in _cues(t)[1].raw_content
