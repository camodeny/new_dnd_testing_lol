"""Unit fixtures for #227 — authoritative conditions, resources, concentration, death saves."""

import datetime
import uuid

import pytest

from app.rules.mechanics import get_character_mechanics_for_sheet
from app.rules.state import (
    CONCENTRATION_CHANGED_EVENT,
    CONDITION_CHANGED_EVENT,
    DEATH_SAVE_CHANGED_EVENT,
    RESOURCE_CHANGED_EVENT,
    StateError,
    StateLedger,
    active_condition_names,
    add_condition,
    apply_state_consequence,
    attacker_condition_sources,
    break_concentration,
    build_concentration_effect,
    build_condition_effect,
    build_death_save_effect,
    build_resource_effect,
    concentration_domain_event,
    condition_domain_event,
    death_save_domain_event,
    defender_granted_sources,
    has_condition,
    parse_concentration,
    record_death_save,
    remove_condition,
    replace_concentration,
    reset_death_saves,
    resource_domain_event,
    restore_resource,
    restore_spell_slot,
    set_exhaustion,
    set_resource,
    spend_resource,
    spend_spell_slot,
    start_concentration,
    tick_conditions,
    update_condition,
)


class FakeSheet:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", uuid.uuid4())
        self.character_id = kwargs.pop("character_id", uuid.uuid4())
        self.owner_id = kwargs.pop("owner_id", uuid.uuid4())
        self.character_name = kwargs.pop("character_name", "Test Hero")
        self.level = kwargs.pop("level", 1)
        self.strength = kwargs.pop("strength", 10)
        self.dexterity = kwargs.pop("dexterity", 10)
        self.constitution = kwargs.pop("constitution", 10)
        self.intelligence = kwargs.pop("intelligence", 10)
        self.wisdom = kwargs.pop("wisdom", 10)
        self.charisma = kwargs.pop("charisma", 10)
        self.proficiency_bonus = kwargs.pop("proficiency_bonus", 2)
        for ab in ["str", "dex", "con", "int", "wis", "cha"]:
            setattr(self, f"{ab}_save_prof", kwargs.pop(f"{ab}_save_prof", False))
        for s in [
            "acrobatics", "animal_handling", "arcana", "athletics", "deception",
            "history", "insight", "intimidation", "investigation", "medicine",
            "nature", "perception", "performance", "persuasion", "religion",
            "sleight_of_hand", "stealth", "survival",
        ]:
            setattr(self, f"{s}_prof", kwargs.pop(f"{s}_prof", False))
        self.skill_expertise = kwargs.pop("skill_expertise", None)
        self.skills = kwargs.pop("skills", None)
        self.saving_throws = kwargs.pop("saving_throws", None)
        self.passive_perception = kwargs.pop("passive_perception", None)
        self.armor_class = kwargs.pop("armor_class", 10)
        self.initiative_bonus = kwargs.pop("initiative_bonus", 0)
        self.speed = kwargs.pop("speed", 30)
        self.speed_details = kwargs.pop("speed_details", None)
        self.hit_points_max = kwargs.pop("hit_points_max", 10)
        self.hit_points_current = kwargs.pop("hit_points_current", 10)
        self.hit_points_temp = kwargs.pop("hit_points_temp", 0)
        self.death_save_successes = kwargs.pop("death_save_successes", 0)
        self.death_save_failures = kwargs.pop("death_save_failures", 0)
        self.exhaustion_level = kwargs.pop("exhaustion_level", 0)
        self.inspiration = kwargs.pop("inspiration", False)
        self.weapons = kwargs.pop("weapons", None)
        self.attacks = kwargs.pop("attacks", None)
        self.spellcasting_ability = kwargs.pop("spellcasting_ability", None)
        self.spell_save_dc = kwargs.pop("spell_save_dc", None)
        self.spell_attack_bonus = kwargs.pop("spell_attack_bonus", None)
        self.spell_slots = kwargs.pop("spell_slots", None)
        self.resources = kwargs.pop("resources", None)
        self.conditions = kwargs.pop("conditions", None)
        self.extras = kwargs.pop("extras", None)
        self.classes = kwargs.pop("classes", None)
        for k, v in kwargs.items():
            setattr(self, k, v)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.updated_at = now
        self.created_at = now


# ── Conditions: add / remove / update ─────────────────────────────────────


def test_condition_add_remove_roundtrip():
    conditions, record = add_condition(
        [], name="Poisoned", source="giant spider venom",
        duration_rounds=10, mutation_id="cond-add-1",
    )
    assert record.name == "poisoned"
    assert record.duration_label() == "10 rounds"
    assert has_condition(conditions, "poisoned") is True
    assert active_condition_names(conditions) == ["poisoned"]

    # Structural state feeds later mechanical queries — not narration text.
    sheet = FakeSheet(conditions=conditions)
    mechanics = get_character_mechanics_for_sheet(sheet)
    assert [c.condition_name for c in mechanics.conditions] == ["poisoned"]
    assert mechanics.conditions[0].source == "giant spider venom"
    assert mechanics.conditions[0].duration_remaining == "10 rounds"

    remaining, removed = remove_condition(conditions, name="POISONED", mutation_id="cond-rm-1")
    assert removed.name == "poisoned"
    assert remaining == []
    assert has_condition(remaining, "poisoned") is False


def test_condition_add_normalizes_sheet_compatible_shape():
    conditions, _ = add_condition(
        None, name="frightened", source="dragon fear",
        save_ends={"ability": "wisdom", "dc": 15}, mutation_id="cond-shape-1",
    )
    entry = conditions[0]
    # #224 mechanics keys preserved…
    assert entry["condition_name"] == "frightened"
    assert entry["source"] == "dragon fear"
    assert entry["duration_remaining"] == "save ends (wisdom 15)"
    # …plus normalized provenance/expiry semantics.
    assert entry["save_ends"] == {"ability": "wisdom", "dc": 15}
    assert entry["provenance"] == {}
    sheet = FakeSheet(conditions=conditions)
    assert get_character_mechanics_for_sheet(sheet).conditions[0].condition_name == "frightened"


def test_condition_invalid_transitions_rejected_pre_commit():
    with pytest.raises(StateError) as exc:
        add_condition([], name="Electrified", source="storm", mutation_id="bad-1")
    assert exc.value.code == "invalid_condition"

    with pytest.raises(StateError) as exc:
        add_condition([], name="poisoned", source="  ", mutation_id="bad-2")
    assert exc.value.code == "missing_source"

    present, _ = add_condition([], name="stunned", source="monk", mutation_id="ok-1")
    with pytest.raises(StateError) as exc:
        add_condition(present, name="stunned", source="monk", mutation_id="ok-2")
    assert exc.value.code == "duplicate_condition"  # update instead of re-adding

    with pytest.raises(StateError) as exc:
        remove_condition(present, name="blinded", mutation_id="bad-3")
    assert exc.value.code == "condition_not_present"

    with pytest.raises(StateError) as exc:
        update_condition(present, name="blinded", mutation_id="bad-4")
    assert exc.value.code == "condition_not_present"

    with pytest.raises(StateError) as exc:
        add_condition([], name="poisoned", source="x", duration_rounds=0, mutation_id="bad-5")
    assert exc.value.code == "invalid_duration"

    with pytest.raises(StateError) as exc:
        add_condition([], name="x", source="x", mutation_id="   ")
    assert exc.value.code in ("missing_mutation_id", "invalid_condition")


def test_condition_update_and_mechanical_candidates():
    conditions, _ = add_condition([], name="poisoned", source="venom", duration_rounds=10, mutation_id="upd-1")
    updated, record = update_condition(
        conditions, name="poisoned", duration_rounds=3,
        save_ends={"ability": "constitution", "dc": 12}, mutation_id="upd-2",
    )
    assert record.duration_label() == "3 rounds or save ends (constitution 12)"
    assert has_condition(updated, "poisoned") is True

    sources = attacker_condition_sources(updated)
    assert sources["disadvantage"] == ["poisoned"]
    assert sources["cannot_act"] == []

    prone, _ = add_condition([], name="prone", source="shove", mutation_id="upd-3")
    assert attacker_condition_sources(prone)["disadvantage"] == ["prone"]
    assert defender_granted_sources(prone)["advantage"] == ["prone"]

    invisible, _ = add_condition([], name="invisible", source="spell", mutation_id="upd-4")
    assert defender_granted_sources(invisible)["disadvantage"] == ["invisible"]

    stunned, _ = add_condition([], name="stunned", source="monk", mutation_id="upd-5")
    assert attacker_condition_sources(stunned)["cannot_act"] == ["stunned"]


def test_condition_duration_tick_hook():
    timed, _ = add_condition([], name="frightened", source="fear", duration_rounds=2, mutation_id="tick-1")
    save_only, _ = add_condition([], name="poisoned", source="venom", save_ends={"ability": "constitution", "dc": 12}, mutation_id="tick-2")
    forever, _ = add_condition([], name="petrified", source="medusa", is_permanent=True, mutation_id="tick-3")
    all_conds = timed + save_only + forever

    remaining, expired = tick_conditions(all_conds, rounds=1, mutation_id="tick-4")
    assert [e.name for e in expired] == []
    assert has_condition(remaining, "frightened") is True

    remaining, expired = tick_conditions(remaining, rounds=1, mutation_id="tick-5")
    assert [e.name for e in expired] == ["frightened"]
    assert has_condition(remaining, "frightened") is False
    # Save-ends-only and permanent entries never expire on a tick.
    assert has_condition(remaining, "poisoned") is True
    assert has_condition(remaining, "petrified") is True

    with pytest.raises(StateError) as exc:
        tick_conditions(remaining, rounds=0, mutation_id="tick-6")
    assert exc.value.code == "invalid_duration"


# ── Resources + spell slots ───────────────────────────────────────────────


def _tracked_resources():
    return [
        {"name": "Ki", "current": 3, "maximum": 5, "recharge": "short_rest"},
        {"name": "Rage", "current": 2, "max": 2},
    ]


def test_resource_spend_restore_set_bounds():
    after, delta = spend_resource(_tracked_resources(), name="ki", amount=2, mutation_id="res-1")
    assert delta == {"before": 3, "spent": 2, "after": 1, "maximum": 5}

    with pytest.raises(StateError) as exc:
        spend_resource(after, name="ki", amount=2, mutation_id="res-2")
    assert exc.value.code == "insufficient_resource"  # cannot go below 0

    restored, delta = restore_resource(after, name="ki", amount=10, mutation_id="res-3")
    assert delta["after"] == 5  # capped at maximum
    assert delta["restored"] == 4

    settled, delta = set_resource(restored, name="rage", current=1, mutation_id="res-4")
    assert delta["after"] == 1
    with pytest.raises(StateError) as exc:
        set_resource(settled, name="rage", current=9, mutation_id="res-5")
    assert exc.value.code == "invalid_resource_bounds"  # cannot exceed max

    with pytest.raises(StateError) as exc:
        spend_resource(settled, name="Lay on Hands", mutation_id="res-6")
    assert exc.value.code == "resource_not_found"


def test_spell_slot_spend_restore_idempotent_shape():
    slots = {"1": {"max": 4, "used": 3, "remaining": 1}, "2": {"max": 3, "used": 0, "remaining": 3}}
    spent, delta = spend_spell_slot(slots, level=1, mutation_id="slot-1")
    assert delta["remaining"] == 0
    assert spent["1"] == {"max": 4, "used": 4, "remaining": 0}

    with pytest.raises(StateError) as exc:
        spend_spell_slot(spent, level=1, mutation_id="slot-2")
    assert exc.value.code == "no_slots_remaining"

    with pytest.raises(StateError) as exc:
        spend_spell_slot(spent, level=9, mutation_id="slot-3")
    assert exc.value.code == "unknown_slot_level"

    restored, delta = restore_spell_slot(spent, level=1, amount=2, mutation_id="slot-4")
    assert restored["1"]["used"] == 2
    assert delta["restored"] == 2

    # Structural slots feed later mechanical queries.
    sheet = FakeSheet(spell_slots=restored)
    assert get_character_mechanics_for_sheet(sheet).spellcasting.slots["1"]["remaining"] == 2


# ── Concentration ─────────────────────────────────────────────────────────


def test_concentration_start_replace_break():
    state, started = start_concentration(
        None, effect_name="Bless", effect_id="eff-bless",
        source="spell: bless", mutation_id="conc-1",
    )
    assert started.effect_name == "Bless"
    assert parse_concentration(state).active is True

    with pytest.raises(StateError) as exc:
        start_concentration(state, effect_name="Bane", effect_id="eff-bane", source="spell", mutation_id="conc-2")
    assert exc.value.code == "concentration_conflict"  # replace instead

    replaced, now, broke = replace_concentration(
        state, effect_name="Bane", effect_id="eff-bane",
        source="spell: bane", mutation_id="conc-3",
    )
    assert broke is not None and broke.effect_name == "Bless"
    assert now.effect_name == "Bane"

    cleared, was = break_concentration(replaced, reason="damage", mutation_id="conc-4")
    assert was.effect_name == "Bane"
    assert parse_concentration(cleared).active is False

    with pytest.raises(StateError) as exc:
        break_concentration(cleared, reason="damage", mutation_id="conc-5")
    assert exc.value.code == "no_concentration"  # breaking nothing fails closed

    with pytest.raises(StateError) as exc:
        break_concentration(replaced, reason="distracted", mutation_id="conc-6")
    assert exc.value.code == "invalid_concentration_reason"


def test_concentration_replace_on_empty_behaves_as_start():
    replaced, now, broke = replace_concentration(
        None, effect_name="Hunter's Mark", effect_id="eff-hm",
        source="spell", mutation_id="conc-7",
    )
    assert broke is None
    assert now.effect_name == "Hunter's Mark"
    assert parse_concentration(replaced).active is True


# ── Death saves ───────────────────────────────────────────────────────────


def test_death_save_progression_reset():
    s = record_death_save(0, 0, result="success", mutation_id="ds-1")
    assert (s.successes, s.failures, s.outcome) == (1, 0, "ongoing")
    s = record_death_save(s.successes, s.failures, result="failure", mutation_id="ds-2")
    assert (s.successes, s.failures) == (1, 1)

    s = record_death_save(s.successes, s.failures, result="success", mutation_id="ds-3")
    s = record_death_save(s.successes, s.failures, result="success", mutation_id="ds-4")
    assert s.outcome == "stabilized" and s.stabilized is True

    with pytest.raises(StateError) as exc:
        record_death_save(s.successes, s.failures, result="success", mutation_id="ds-5")
    assert exc.value.code == "death_save_resolved"  # resolved counters fail closed

    cleared = reset_death_saves(s.successes, s.failures, reason="stabilized", mutation_id="ds-6")
    assert (cleared.successes, cleared.failures) == (0, 0)


def test_death_save_death_nat1_nat20():
    s = record_death_save(0, 2, result="critical_failure", mutation_id="ds-7")
    assert s.failures == 3  # natural 1 counts double but clamps at the 0-3 canonical bound
    assert s.outcome == "dead" and s.dead is True

    # A clamped terminal counter stays resettable via the canonical reset path.
    cleared = reset_death_saves(s.successes, s.failures, reason="revived", mutation_id="ds-7b")
    assert (cleared.successes, cleared.failures) == (0, 0)

    revived = record_death_save(1, 1, result="critical_success", mutation_id="ds-8")
    assert revived.outcome == "revived"
    assert revived.revived_hp == 1
    assert (revived.successes, revived.failures) == (0, 0)

    with pytest.raises(StateError):
        record_death_save(0, 0, result="maybe", mutation_id="ds-9")
    with pytest.raises(StateError):
        reset_death_saves(0, 0, reason="vibes", mutation_id="ds-10")


def test_exhaustion_bounds():
    assert set_exhaustion(3, mutation_id="ex-1") == 3
    with pytest.raises(StateError) as exc:
        set_exhaustion(11, mutation_id="ex-2")
    assert exc.value.code == "invalid_exhaustion"


# ── Duplicate-application guard ───────────────────────────────────────────


def test_state_ledger_cannot_apply_twice():
    ledger = StateLedger()
    assert ledger.apply("mut-abc") == "applied"
    assert ledger.apply("mut-abc") == "duplicate"
    assert ledger.is_duplicate("mut-abc") is True
    assert len(ledger) == 1


def _sqlite_db():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    from sqlalchemy.orm import sessionmaker

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore
    from database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_durable_duplicate_retry_cannot_double_spend():
    import uuid as _uuid
    from models.profiles import Profile

    factory = _sqlite_db()
    actor = _uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=actor, email="state-ledger@example.com"))
        db.commit()

    resources = _tracked_resources()
    spent, _delta = spend_resource(resources, name="ki", amount=1, mutation_id="durable-mut-1")
    payload = {"mutation_id": "durable-mut-1", "resources": spent}
    calls: list[int] = []
    with factory() as db:
        first, replayed = apply_state_consequence(
            db, actor_id=actor, scope_type="campaign", scope_id="camp-a",
            mutation_id="durable-mut-1", payload=payload,
            execute=lambda: calls.append(1) or {"resources": spent},
        )
        assert replayed is False
    with factory() as restarted_db:  # new session = restart/worker boundary
        second, replayed_second = apply_state_consequence(
            restarted_db, actor_id=actor, scope_type="campaign", scope_id="camp-a",
            mutation_id="durable-mut-1", payload=payload,
            execute=lambda: (_ for _ in ()).throw(AssertionError("must not re-execute on retry")),
        )
        assert replayed_second is True
    assert first == second
    assert calls == [1]


def test_durable_same_id_different_payload_is_conflict():
    import uuid as _uuid
    from app.idempotency import IdempotencyConflictError
    from models.profiles import Profile

    factory = _sqlite_db()
    actor = _uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=actor, email="state-conflict@example.com"))
        db.commit()
    with factory() as db:
        apply_state_consequence(
            db, actor_id=actor, scope_type="campaign", scope_id="camp-b",
            mutation_id="durable-mut-2", payload={"n": 1},
            execute=lambda: {"applied": True},
        )
        with pytest.raises(IdempotencyConflictError):
            apply_state_consequence(
                db, actor_id=actor, scope_type="campaign", scope_id="camp-b",
                mutation_id="durable-mut-2", payload={"n": 2},
                execute=lambda: {"applied": "tampered"},
            )


# ── Domain events: hidden NPC redaction ───────────────────────────────────


def test_hidden_npc_state_stays_dm_only_but_structural():
    change = {
        "target_kind": "npc", "target_id": str(uuid.uuid4()), "op": "add",
        "condition": "poisoned", "mutation_id": "hide-1", "visibility": "dm_private",
    }
    event_type, public, visibility = condition_domain_event(change, include_private=False)
    assert event_type == CONDITION_CHANGED_EVENT
    assert visibility == "dm_private"
    assert "poisoned" not in str(public)  # hidden specifics redacted
    assert public["redacted"] is True

    _, private, private_vis = condition_domain_event(change, include_private=True)
    assert private_vis == "dm_private"
    assert private["condition"] == "poisoned"  # DM audit keeps specifics

    # …while the hidden state still drives mechanics on the stored details.
    assert has_condition([{"condition_name": "poisoned", "source": "venom"}], "poisoned") is True
    assert attacker_condition_sources([{"condition_name": "poisoned", "source": "v"}])["disadvantage"] == ["poisoned"]


def test_domain_event_builders_and_visibility_pairing():
    res_change = {
        "target_kind": "pc", "target_id": str(uuid.uuid4()), "op": "spend",
        "resource": "Ki", "delta": {"before": 3, "after": 2}, "mutation_id": "evt-1",
        "visibility": "public",
    }
    event_type, public, visibility = resource_domain_event(res_change, include_private=False, disclosed=True)
    assert event_type == RESOURCE_CHANGED_EVENT
    assert visibility == "public"
    assert public["delta"] == {"before": 3, "after": 2}

    _, private, private_vis = resource_domain_event(res_change, include_private=True)
    assert private_vis == "dm_private"  # private payload never rides public

    conc_change = {
        "target_kind": "pc", "target_id": str(uuid.uuid4()), "op": "break",
        "effect_name": "Bless", "active": False, "mutation_id": "evt-2", "visibility": "dm_private",
    }
    event_type, _payload, visibility = concentration_domain_event(conc_change, include_private=False)
    assert event_type == CONCENTRATION_CHANGED_EVENT
    assert visibility == "dm_private"

    ds_change = {
        "target_kind": "pc", "target_id": str(uuid.uuid4()), "op": "record",
        "successes": 1, "failures": 0, "outcome": "ongoing",
        "mutation_id": "evt-3", "visibility": "public",
    }
    event_type, public, _vis = death_save_domain_event(ds_change, include_private=False, disclosed=True)
    assert event_type == DEATH_SAVE_CHANGED_EVENT
    assert public["outcome"] == "ongoing"
    assert public["successes"] == 1


# ── Staged-effect builders + contract + provider guard ────────────────────


def test_state_effect_builders_require_stable_ids():
    target = str(uuid.uuid4())
    effect = build_condition_effect(
        effect_id="cond-eff-1", mutation_id="mut-1", target_kind="pc",
        target_id=target, op="add", condition="poisoned", source="venom",
    )
    assert effect["effect_type"] == "apply_condition"
    assert effect["arguments"]["mutation_id"] == "mut-1"
    assert effect["arguments"]["visibility"] == "dm_private"  # fail-closed default

    effect = build_resource_effect(
        effect_id="res-eff-1", mutation_id="mut-2", target_kind="pc",
        target_id=target, op="spend", resource="Ki", amount=1,
    )
    assert effect["effect_type"] == "apply_resource"

    effect = build_concentration_effect(
        effect_id="conc-eff-1", mutation_id="mut-3", target_kind="pc",
        target_id=target, op="start", effect_name="Bless",
        concentration_effect_id="eff-bless", source="spell",
    )
    assert effect["effect_type"] == "apply_concentration"

    effect = build_death_save_effect(
        effect_id="ds-eff-1", mutation_id="mut-4", target_kind="pc",
        target_id=target, op="record", result="success",
    )
    assert effect["effect_type"] == "apply_death_save"

    with pytest.raises(StateError):
        build_condition_effect(
            effect_id="bad!!", mutation_id="mut-5", target_kind="pc",
            target_id=target, op="add", condition="poisoned", source="v",
        )
    with pytest.raises(StateError):
        build_resource_effect(
            effect_id="res-eff-2", mutation_id="mut-6", target_kind="pc",
            target_id=target, op="set", slot_level=3,
        )  # slots support spend/restore only


def test_built_effects_survive_canonical_contract_validation():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    target = str(uuid.uuid4())
    effects = [
        build_condition_effect(
            effect_id="ctr-cond", mutation_id="ctr-m1", target_kind="pc",
            target_id=target, op="add", condition="poisoned", source="venom",
        ),
        build_resource_effect(
            effect_id="ctr-res", mutation_id="ctr-m2", target_kind="pc",
            target_id=target, op="spend", resource="Ki",
        ),
        build_concentration_effect(
            effect_id="ctr-conc", mutation_id="ctr-m3", target_kind="pc",
            target_id=target, op="break", reason="damage",
        ),
        build_death_save_effect(
            effect_id="ctr-ds", mutation_id="ctr-m4", target_kind="pc",
            target_id=target, op="record", result="failure",
        ),
    ]
    claim = {
        "text": "Venom courses through the fighter.",
        "claim_kind": "observation",
        "origin": "dm_adjudication",
        "visibility": "public",
    }
    c = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "state changes",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [claim]}],
            "staged_effects": effects,
        }
    )
    assert [e.effect_type for e in c.staged_effects] == [
        "apply_condition", "apply_resource", "apply_concentration", "apply_death_save",
    ]


def test_provider_schema_excludes_code_built_state_effects():
    from app.dm.contract import contract_json_schema_strict

    schema = contract_json_schema_strict()
    effect_type_enum = schema["$defs"]["StagedEffect"]["properties"]["effect_type"]["enum"]
    for code_built in ("apply_attack_damage", "apply_condition", "apply_resource", "apply_concentration", "apply_death_save"):
        assert code_built not in effect_type_enum
    assert "start_encounter" in effect_type_enum  # provider effects untouched


def test_provider_authored_state_is_rejected_by_rules_validator():
    from app.dm.validators import RulesValidator

    target = str(uuid.uuid4())

    def _contract(*effects):
        from app.dm.contract import CONTRACT_VERSION, normalize_contract

        claim = {
            "text": "Steel flashes.",
            "claim_kind": "observation",
            "origin": "dm_adjudication",
            "visibility": "public",
        }
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "combat beat",
                "beats": [{"id": "beat_1", "type": "narration", "claims": [claim]}],
                "staged_effects": list(effects),
            }
        )

    forged = build_condition_effect(
        effect_id="forge-cond", mutation_id="forge-m1", target_kind="pc",
        target_id=target, op="add", condition="poisoned", source="model says so",
    )
    result = RulesValidator().validate(_contract(forged), object())
    assert result.passed is False
    assert [v.code for v in result.violations] == ["provider_authored_rules_state"]
    assert _contract().staged_effects == [] or RulesValidator().validate(_contract(), object()).passed is True


# ── Staged-effect promotion (PC sheet + NPC entity, transactional) ────────


def _handler_db():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    from sqlalchemy.orm import sessionmaker

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore
    from database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _handler_fixture(db):
    import uuid as _uuid
    from models.campaigns import Campaign, CampaignMember
    from models.characters import Character, Dnd5eCharacterSheet
    from models.dm import DmTurn, DmTurnAttempt
    from models.profiles import Profile
    from models.world import WorldEntity

    owner = _uuid.uuid4()
    db.add(Profile(id=owner, email="state-handler@example.com"))
    camp = Campaign(id=_uuid.uuid4(), owner_id=owner, name="State", revision=0)
    db.add(camp)
    db.flush()
    char = Character(id=_uuid.uuid4(), owner_id=owner, system="dnd5e", name="Fighter")
    db.add(char)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner", selected_character_id=char.id))
    sheet = Dnd5eCharacterSheet.from_frontend(
        {
            "name": "Fighter",
            "total_level": 3,
            "max_hp": 24,
            "current_hp": 0,
            "temp_hp": 0,
            "resources": [{"name": "Ki", "current": 3, "maximum": 5}],
            "spell_slots": {"1": {"max": 4, "used": 0}},
            "conditions": [],
        },
        owner_id=owner,
    )
    sheet.character_id = char.id
    db.add(sheet)
    brute = WorldEntity(
        id=_uuid.uuid4(),
        campaign_id=camp.id,
        entity_type="monster",
        name="Brute",
        status="active",
        visibility="dm_only",
        details={
            "hit_points": {"current": 30, "maximum": 30, "temporary": 0},
            "resources": [{"name": "Rage", "current": 2, "maximum": 2}],
            "conditions": [],
        },
    )
    db.add(brute)
    db.flush()
    turn = DmTurn(
        id=_uuid.uuid4(), campaign_id=camp.id, thread_id=f"thread:{_uuid.uuid4()}",
        audience="campaign", status="pending", source_revision=0,
        input_set_revision=1, submission_ids=[],
    )
    db.add(turn)
    db.flush()
    attempt = DmTurnAttempt(
        id=_uuid.uuid4(), turn_id=turn.id, attempt_number=1, status="prepared",
        campaign_id=camp.id, thread_id=turn.thread_id, audience="campaign",
        source_revision=0, input_set_revision=1, submission_ids=[],
    )
    db.add(attempt)
    db.commit()
    return camp, turn, attempt, char, brute


def _sheet_for(db, char_id):
    from models.characters import Dnd5eCharacterSheet
    from sqlalchemy import select

    return db.execute(
        select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char_id)
    ).scalars().first()


def test_staged_condition_applies_to_pc_sheet_and_mechanics():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        effect = build_condition_effect(
            effect_id="h-cond-1", mutation_id="h-mut-1", target_kind="pc",
            target_id=str(char.id), op="add", condition="poisoned",
            source="venom", duration_rounds=10,
        )
        apply_staged_effects(db, camp, [effect], turn, attempt)
        db.commit()
        sheet = _sheet_for(db, char.id)
        assert has_condition(sheet.conditions, "poisoned") is True
        # Canonical store feeds #224 reads: narration is not the only record.
        mechanics = get_character_mechanics_for_sheet(sheet)
        assert [c.condition_name for c in mechanics.conditions] == ["poisoned"]


def test_staged_resource_spend_and_slot_spend_on_pc():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        apply_staged_effects(db, camp, [
            build_resource_effect(
                effect_id="h-res-1", mutation_id="h-mut-2", target_kind="pc",
                target_id=str(char.id), op="spend", resource="Ki", amount=2,
            ),
            build_resource_effect(
                effect_id="h-res-2", mutation_id="h-mut-3", target_kind="pc",
                target_id=str(char.id), op="spend", slot_level=1,
            ),
        ], turn, attempt)
        db.commit()
        sheet = _sheet_for(db, char.id)
        assert sheet.resources[0]["current"] == 1
        assert sheet.spell_slots["1"]["used"] == 1


def test_staged_concentration_start_replace_break_on_pc():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        apply_staged_effects(db, camp, [
            build_concentration_effect(
                effect_id="h-conc-1", mutation_id="h-mut-4", target_kind="pc",
                target_id=str(char.id), op="start", effect_name="Bless",
                concentration_effect_id="eff-bless", source="spell",
            ),
        ], turn, attempt)
        db.commit()
        assert parse_concentration(_sheet_for(db, char.id).extras["concentration"]).effect_name == "Bless"

        apply_staged_effects(db, camp, [
            build_concentration_effect(
                effect_id="h-conc-2", mutation_id="h-mut-5", target_kind="pc",
                target_id=str(char.id), op="replace", effect_name="Bane",
                concentration_effect_id="eff-bane", source="spell",
            ),
        ], turn, attempt)
        db.commit()
        assert parse_concentration(_sheet_for(db, char.id).extras["concentration"]).effect_name == "Bane"

        apply_staged_effects(db, camp, [
            build_concentration_effect(
                effect_id="h-conc-3", mutation_id="h-mut-6", target_kind="pc",
                target_id=str(char.id), op="break", reason="damage",
            ),
        ], turn, attempt)
        db.commit()
        assert parse_concentration(_sheet_for(db, char.id).extras.get("concentration")).active is False


def test_incapacitating_condition_breaks_concentration_hook():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        apply_staged_effects(db, camp, [
            build_concentration_effect(
                effect_id="h-conc-4", mutation_id="h-mut-7", target_kind="pc",
                target_id=str(char.id), op="start", effect_name="Bless",
                concentration_effect_id="eff-bless", source="spell",
            ),
        ], turn, attempt)
        db.commit()
        apply_staged_effects(db, camp, [
            build_condition_effect(
                effect_id="h-cond-2", mutation_id="h-mut-8", target_kind="pc",
                target_id=str(char.id), op="add", condition="stunned", source="monk",
            ),
        ], turn, attempt)
        db.commit()
        sheet = _sheet_for(db, char.id)
        assert has_condition(sheet.conditions, "stunned") is True
        assert parse_concentration((sheet.extras or {}).get("concentration")).active is False


def test_staged_death_save_progression_reset_and_hooks():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        apply_staged_effects(db, camp, [
            build_death_save_effect(
                effect_id="h-ds-1", mutation_id="h-mut-9", target_kind="pc",
                target_id=str(char.id), op="record", result="failure",
            ),
        ], turn, attempt)
        db.commit()
        sheet = _sheet_for(db, char.id)
        # Baseline hook: first save at 0 HP structurally records unconscious.
        assert (sheet.death_save_successes, sheet.death_save_failures) == (0, 1)
        assert has_condition(sheet.conditions, "unconscious") is True
        assert get_character_mechanics_for_sheet(sheet).combat["death_saves"] == {"successes": 0, "failures": 1}

        apply_staged_effects(db, camp, [
            build_death_save_effect(
                effect_id="h-ds-2", mutation_id="h-mut-10", target_kind="pc",
                target_id=str(char.id), op="record", result="critical_success",
            ),
        ], turn, attempt)
        db.commit()
        sheet = _sheet_for(db, char.id)
        # Natural-20 hook: 1 HP restored, counters reset, conscious again.
        assert (sheet.death_save_successes, sheet.death_save_failures) == (0, 0)
        assert sheet.hit_points_current == 1
        assert has_condition(sheet.conditions, "unconscious") is False


def test_staged_npc_hidden_state_applies_and_stays_structural():
    from app.dm.effects import apply_staged_effects
    from models.world import WorldEntity

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, brute = _handler_fixture(db)
        apply_staged_effects(db, camp, [
            build_condition_effect(
                effect_id="h-npc-1", mutation_id="h-mut-11", target_kind="npc",
                target_id=str(brute.id), op="add", condition="poisoned", source="venom",
            ),
            build_resource_effect(
                effect_id="h-npc-2", mutation_id="h-mut-12", target_kind="npc",
                target_id=str(brute.id), op="spend", resource="Rage",
            ),
        ], turn, attempt)
        db.commit()
        row = db.get(WorldEntity, brute.id)
        assert has_condition(row.details["conditions"], "poisoned") is True
        assert row.details["resources"][0]["current"] == 1


def test_campaign_visible_npc_dm_private_state_redacted_for_members():
    """DM-private mutations on a member-visible NPC stay structural but hidden (#227)."""
    import uuid as _uuid

    from app.dm.effects import apply_staged_effects
    from app.rules.state import project_npc_details_for_viewer
    from app.world.service import project_entity_for_viewer
    from models.world import WorldEntity

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, _brute = _handler_fixture(db)
        visible = WorldEntity(
            id=_uuid.uuid4(),
            campaign_id=camp.id,
            entity_type="monster",
            name="Scout",
            status="active",
            visibility="campaign",
            details={
                "hit_points": {"current": 12, "maximum": 12, "temporary": 0},
                "resources": [{"name": "Rage", "current": 2, "maximum": 2}],
                "conditions": [],
                "spell_slots": {"1": {"max": 2, "used": 0}},
                "death_saves": {"successes": 0, "failures": 0},
                "exhaustion_level": 0,
            },
        )
        db.add(visible)
        db.commit()
        apply_staged_effects(db, camp, [
            build_condition_effect(
                effect_id="vis-cond", mutation_id="vis-m1", target_kind="npc",
                target_id=str(visible.id), op="add", condition="poisoned",
                source="venom",
            ),
            build_resource_effect(
                effect_id="vis-res", mutation_id="vis-m2", target_kind="npc",
                target_id=str(visible.id), op="spend", resource="Rage",
            ),
            build_concentration_effect(
                effect_id="vis-conc", mutation_id="vis-m3", target_kind="npc",
                target_id=str(visible.id), op="start", effect_name="Hex",
                concentration_effect_id="eff-hex", source="spell",
            ),
            build_death_save_effect(
                effect_id="vis-ds", mutation_id="vis-m4", target_kind="npc",
                target_id=str(visible.id), op="record", result="failure",
            ),
        ], turn, attempt)
        db.commit()
        row = db.get(WorldEntity, visible.id)
        # Authority lane keeps the full structural state for mechanics/audit.
        assert has_condition(row.details["conditions"], "poisoned") is True
        assert row.details["conditions"][0]["visibility"] == "dm_private"
        assert row.details["resources"][0]["current"] == 1
        assert row.details["concentration"]["effect_name"] == "Hex"
        assert row.details["death_saves"] == {"successes": 0, "failures": 1}

        member_details = project_npc_details_for_viewer(row.details, False)
        assert "poisoned" not in str(member_details)
        assert member_details["conditions"] == []
        assert member_details["resources"] == []
        assert member_details["spell_slots"] == {} or "Hex" not in str(member_details.get("concentration"))
        assert member_details.get("concentration", {}).get("active") is False
        assert "Hex" not in str(member_details)
        assert member_details["death_saves"] == {"successes": 0, "failures": 0}
        assert "rules_state_visibility" not in member_details

        member_view = project_entity_for_viewer(row, False)
        assert "poisoned" not in str(member_view["details"])
        assert "Hex" not in str(member_view["details"])
        assert member_view["visibility"] == "campaign"

        authority_view = project_entity_for_viewer(row, True)
        assert has_condition(authority_view["details"]["conditions"], "poisoned") is True


def test_multi_effect_mutation_is_transactional_with_source_turn():
    """One invalid entry rejects the whole staged batch before any write."""
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        good = build_condition_effect(
            effect_id="tx-cond", mutation_id="tx-m1", target_kind="pc",
            target_id=str(char.id), op="add", condition="poisoned", source="venom",
        )
        bad = build_resource_effect(
            effect_id="tx-res", mutation_id="tx-m2", target_kind="pc",
            target_id=str(char.id), op="spend", resource="Ki", amount=99,
        )
        with pytest.raises(ValueError, match="invalid resource transition"):
            apply_staged_effects(db, camp, [good, bad], turn, attempt)
        db.rollback()
        sheet = _sheet_for(db, char.id)
        assert sheet.conditions in (None, [])  # good entry rolled back with the batch
        assert sheet.resources[0]["current"] == 3


def test_duplicate_mutation_id_in_one_commit_rejected_pre_write():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        target = str(char.id)
        first = build_condition_effect(
            effect_id="dup-a", mutation_id="dup-mut", target_kind="pc",
            target_id=target, op="add", condition="poisoned", source="a",
        )
        second = build_condition_effect(
            effect_id="dup-b", mutation_id="dup-mut", target_kind="pc",
            target_id=target, op="add", condition="blinded", source="b",
        )
        with pytest.raises(ValueError, match="Duplicate logical mutation_id"):
            apply_staged_effects(db, camp, [first, second], turn, attempt)
        db.rollback()
        assert _sheet_for(db, char.id).conditions in (None, [])


def test_staged_effect_rejects_character_outside_campaign_roster():
    import uuid as _uuid
    from app.dm.effects import apply_staged_effects
    from models.characters import Character, Dnd5eCharacterSheet
    from models.profiles import Profile

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, _brute = _handler_fixture(db)
        outsider_owner = _uuid.uuid4()
        db.add(Profile(id=outsider_owner, email="state-outsider@example.com"))
        outsider = Character(id=_uuid.uuid4(), owner_id=outsider_owner, system="dnd5e", name="Outsider")
        db.add(outsider)
        db.flush()
        sheet = Dnd5eCharacterSheet.from_frontend({"name": "Outsider", "total_level": 1}, owner_id=outsider_owner)
        sheet.character_id = outsider.id
        db.add(sheet)
        db.commit()
        effect = build_condition_effect(
            effect_id="xcamp-1", mutation_id="xcamp-m1", target_kind="pc",
            target_id=str(outsider.id), op="add", condition="poisoned", source="venom",
        )
        with pytest.raises(ValueError, match="not on this campaign's active roster"):
            apply_staged_effects(db, camp, [effect], turn, attempt)
