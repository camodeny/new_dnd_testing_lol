"""Issue #229 — mechanics intents become code-built rules effects.

The model states WHAT happens (damage from a hazard, a condition, a resource
spend); code resolves legality, rolls dice, builds the ``apply_*`` effects,
and appends the outcome beat. Each path runs end-to-end through
``execute_dm_attempt`` → ``commit_turn``.
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402

from app.dm.contract import CONTRACT_VERSION, ContractValidationError, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.dm.mechanics import OUTCOME_BEAT_ID, resolve_mechanics, with_outcome_beat  # noqa: E402
from app.dm.streams import reconstruct_text  # noqa: E402
from app.dm.validators import ValidatorRejectionError  # noqa: E402


@pytest.fixture
def table(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mechanics.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = uuid.uuid4()
    with factory() as s:
        camp_id, thread_id = uuid.uuid4(), uuid.uuid4()
        s.add(Profile(id=owner, email="owner@example.com"))
        s.add(Campaign(id=camp_id, owner_id=owner, name="Table", revision=0))
        s.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        char = Character(owner_id=owner, name="Mira", system="dnd5e")
        s.add(char)
        s.flush()
        s.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=owner, character_name="Mira",
            race="Human", char_class="Bard", level=3,
            hit_points_current=20, hit_points_max=20, hit_points_temp=0,
            spell_slots={"1": {"max": 2, "used": 0}, "2": {"max": 1, "used": 1}},
            resources=[{"name": "Bardic Inspiration", "current": 1, "max": 3}],
            conditions=[],
        ))
        s.add(CampaignMember(campaign_id=camp_id, user_id=owner, role="owner", selected_character_id=char.id))
        s.commit()
        yield s, camp_id, thread_id, char.id


def _submit(s, camp_id, thread_id, text="I pull the lever."):
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    accept_submission(
        s, campaign_id=camp_id, user_id=s.get(Campaign, camp_id).owner_id,
        raw_content=text, segments=[{"type": "ic", "text": text}], thread_id=str(thread_id),
    )
    s.commit()
    coord = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    return coord


def _contract(mechanics, text="The floor gives way beneath you."):
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "lever trap",
        "beats": [{
            "id": "beat_1",
            "type": "narration",
            "claims": [{"text": text, "claim_kind": "observation", "origin": "dm_adjudication", "visibility": "public"}],
        }],
        "mechanics": mechanics,
    }


def _mech(char_id, mech_id="mech_1", **fields):
    base = {
        "id": mech_id,
        "kind": "damage",
        "target": {"type": "character", "id": str(char_id)},
        "source": "pit trap",
        "damage_dice": None, "damage_type": None, "heal_dice": None,
        "condition": None, "condition_op": None, "duration_rounds": None,
        "resource": None, "spell_slot_level": None, "amount": None,
    }
    base.update(fields)
    return base


def _run(s, camp_id, thread_id, adjudicate):
    turn, attempt = _submit(s, camp_id, thread_id)
    execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    s.expire_all()
    return s.get(DmTurn, turn.id), s.get(DmTurnAttempt, attempt.id)


def _sheet(s, char_id):
    from app.characters.service import latest_sheet

    return latest_sheet(s, char_id)


def test_hazard_damage_is_rolled_and_applied_by_code(table):
    s, camp_id, thread_id, char_id = table
    contract = _contract([_mech(char_id, damage_dice="2d6", damage_type="bludgeoning")])
    turn, attempt = _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(contract))

    assert turn.status == "succeeded"
    [effect] = [e for e in attempt.staged_effects if e["effect_type"] == "apply_attack_damage"]
    total = effect["arguments"]["damage_total"]
    assert 2 <= total <= 12
    assert effect["arguments"]["visibility"] == "public"
    assert _sheet(s, char_id).hit_points_current == 20 - total
    # The narrator received the code-resolved outcome, numbers included.
    text = reconstruct_text(s, attempt.stream_id)
    assert f"Mira takes {total} bludgeoning damage from pit trap." in text


def test_condition_add_is_code_built(table):
    s, camp_id, thread_id, char_id = table
    contract = _contract([_mech(char_id, kind="condition", condition="Poisoned", condition_op="add", source="spider bite", duration_rounds=10)])
    turn, attempt = _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(contract))

    assert turn.status == "succeeded"
    [record] = _sheet(s, char_id).conditions
    assert record["condition_name"] == "poisoned"
    assert record["source"] == "spider bite"
    assert record["duration_rounds"] == 10


def test_illegal_slot_spend_is_refused_and_retried_without_consuming(table):
    s, camp_id, thread_id, char_id = table
    calls: list[str | None] = []

    def adjudicate(packet, feedback=None):
        # Keeps trying the exhausted level-2 slot until told why it failed.
        calls.append(feedback)
        level = 1 if feedback and "no_slots_remaining" in feedback else 2
        return normalize_contract(_contract(
            [_mech(char_id, kind="spend", spell_slot_level=level, source="Healing Word")],
            text="Mira whispers a word of healing.",
        ))

    turn, attempt = _run(s, camp_id, thread_id, adjudicate)

    assert turn.status == "succeeded"
    assert "no_slots_remaining" in (calls[-1] or "")
    slots = _sheet(s, char_id).spell_slots
    assert slots["2"]["used"] == 1  # untouched: the refused spend consumed nothing
    assert slots["1"]["used"] == 1


def test_resource_spend_and_overdraft_across_intents(table):
    s, camp_id, thread_id, char_id = table
    spend = dict(kind="spend", resource="Bardic Inspiration", amount=1, source="inspiring Tamsin")
    contract = normalize_contract(_contract([_mech(char_id, "mech_1", **spend), _mech(char_id, "mech_2", **spend)]))
    turn = s.get(DmTurn, _submit(s, camp_id, thread_id)[0].id)

    resolution = resolve_mechanics(s, s.get(Campaign, camp_id), turn, contract)

    # Intents on one target resolve cumulatively: the second spend overdraws.
    assert [e["id"] for e in resolution.effects] == ["mech-mech_1"]
    assert [(i.intent_id, i.code) for i in resolution.issues] == [("mech_2", "insufficient_resource")]
    assert _sheet(s, char_id).resources[0]["current"] == 1  # resolution never writes


def test_persistently_illegal_mechanic_fails_without_commit(table):
    s, camp_id, thread_id, char_id = table
    contract = _contract([_mech(char_id, kind="condition", condition="poisoned", condition_op="remove")])

    with pytest.raises(ValidatorRejectionError):
        _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(contract))
    s.expire_all()
    assert _sheet(s, char_id).conditions == []


def test_damage_dice_are_stable_per_turn_and_outcome_beat_is_idempotent(table):
    s, camp_id, thread_id, char_id = table
    contract = normalize_contract(_contract([_mech(char_id, damage_dice="4d10", damage_type="fire")]))
    turn = s.get(DmTurn, _submit(s, camp_id, thread_id)[0].id)
    campaign = s.get(Campaign, camp_id)

    first = resolve_mechanics(s, campaign, turn, contract)
    second = resolve_mechanics(s, campaign, turn, contract)
    assert first.effects == second.effects  # validation, staging and retry agree

    once = with_outcome_beat(contract, first)
    twice = with_outcome_beat(once, second)
    assert [b.id for b in twice.beats] == ["beat_1", OUTCOME_BEAT_ID]
    # The snapshot round-trips (narration-only retry re-normalizes it).
    assert normalize_contract(twice.model_dump(mode="json")).beats[-1].id == OUTCOME_BEAT_ID


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.update(mode="clarify", clarify_question="Which lever?"), "mechanics only valid in respond"),
    (lambda c: c["mechanics"][0].update(damage_dice=None), "requires damage_dice"),
    (lambda c: c["mechanics"][0].update(condition="prone"), "must not set heal/condition"),
    (lambda c: c["mechanics"][0]["target"].update(type="location"), "type=character or type=npc"),
])
def test_contract_rejects_malformed_mechanics(mutate, message):
    raw = _contract([_mech(uuid.uuid4(), damage_dice="1d6", damage_type="fire")])
    mutate(raw)
    with pytest.raises(ContractValidationError) as exc:
        normalize_contract(raw)
    assert message in str(exc.value)


def test_model_cannot_author_rules_effects():
    from app.dm.contract import contract_json_schema_strict

    schema = contract_json_schema_strict()
    effect_types = schema["$defs"]["StagedEffect"]["properties"]["effect_type"]["enum"]
    assert not {t for t in effect_types if t.startswith("apply_")}
    assert "MechanicIntent" in schema["$defs"]


def test_healing_is_rolled_capped_and_applied_by_code(table):
    s, camp_id, thread_id, char_id = table
    sheet = _sheet(s, char_id)
    sheet.hit_points_current = 15
    s.commit()
    contract = _contract(
        [_mech(char_id, kind="heal", heal_dice="2d4+3", source="Healing Word"),
         _mech(char_id, "mech_2", kind="spend", spell_slot_level=1, source="Healing Word")],
        text="Mira whispers a word of healing.",
    )
    turn, attempt = _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(contract))

    assert turn.status == "succeeded"
    [heal] = [e for e in attempt.staged_effects if e["effect_type"] == "apply_healing"]
    assert 5 <= heal["arguments"]["heal_total"] <= 11
    # Capped at max HP 20 even when the roll would overshoot.
    assert _sheet(s, char_id).hit_points_current == min(20, 15 + heal["arguments"]["heal_total"])
    assert _sheet(s, char_id).spell_slots["1"]["used"] == 1
    assert "regains" in reconstruct_text(s, attempt.stream_id)


def test_healing_a_downed_pc_resets_death_saves(table):
    s, camp_id, thread_id, char_id = table
    sheet = _sheet(s, char_id)
    sheet.hit_points_current = 0
    sheet.death_save_failures = 2
    s.commit()
    contract = normalize_contract(_contract([_mech(char_id, kind="heal", heal_dice="1d4+1", source="potion")]))
    turn = s.get(DmTurn, _submit(s, camp_id, thread_id)[0].id)

    resolution = resolve_mechanics(s, s.get(Campaign, camp_id), turn, contract)

    assert not resolution.issues
    assert [e["effect_type"] for e in resolution.effects] == ["apply_death_save", "apply_healing"]
    assert resolution.effects[0]["arguments"]["op"] == "reset"


@pytest.mark.parametrize("resource, amount, source, expected", [
    # Playtest 2026-10-03 produced "spends 1 Bardic Inspiration (Bardic Inspiration to Brannoc)".
    ("Bardic Inspiration", 1, "Bardic Inspiration to Brannoc", "Mira uses Bardic Inspiration to Brannoc."),
    ("Second Wind", 1, "Second Wind", "Mira uses Second Wind."),
    ("Bardic Inspiration", 1, "inspiring Tamsin", "Mira uses Bardic Inspiration for inspiring Tamsin."),
    ("Ki", 2, "Flurry of Blows", "Mira spends 2 uses of Ki for Flurry of Blows."),
])
def test_spend_outcome_reads_as_a_sentence(resource, amount, source, expected):
    from app.dm.mechanics import _spend_text

    assert _spend_text("Mira", resource, amount, source) == expected


def test_condition_and_slot_outcomes_read_as_sentences(table):
    s, camp_id, thread_id, char_id = table
    contract = normalize_contract(_contract([
        _mech(char_id, kind="condition", condition="poisoned", condition_op="add", source="a spider bite"),
        _mech(char_id, "mech_2", kind="spend", spell_slot_level=1, source="Healing Word"),
    ]))
    turn = s.get(DmTurn, _submit(s, camp_id, thread_id)[0].id)

    outcomes = [text for _, text in resolve_mechanics(s, s.get(Campaign, camp_id), turn, contract).outcomes]

    assert outcomes == ["A spider bite leaves Mira poisoned.", "Mira expends a level 1 spell slot on Healing Word."]
