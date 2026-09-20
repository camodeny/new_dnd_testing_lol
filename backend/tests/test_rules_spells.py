"""Unit fixtures for #228 — spellcasting validation + mechanical effect primitives."""

import datetime
import uuid

import pytest

from app.rules.attacks import defender_from_sheet
from app.rules.spells import (
    ActionEconomy,
    SpellError,
    SpellLedger,
    apply_spell_consequence,
    build_spell_condition_effect,
    build_spell_damage_effect,
    cantrip_dice_count,
    damage_expression_for_slot,
    fallback_for_spell,
    get_spell_def,
    healing_expression_for_slot,
    is_spell_supported,
    project_npc_spells_for_viewer,
    query_known_spells,
    query_npc_spells,
    query_spell_slots,
    resolve_spell_attack,
    resolve_spell_damage,
    resolve_spell_healing,
    resolve_spell_save,
    spell_attacker,
    spell_domain_event,
    spell_rule_ref,
    spell_save_dc,
    stage_spell_cast,
    validate_spell_cast,
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
        self.intelligence = kwargs.pop("intelligence", 16)
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
        self.spellcasting_ability = kwargs.pop("spellcasting_ability", "intelligence")
        self.spell_save_dc = kwargs.pop("spell_save_dc", None)
        self.spell_attack_bonus = kwargs.pop("spell_attack_bonus", None)
        self.spell_slots = kwargs.pop("spell_slots", None)
        self.spells = kwargs.pop("spells", None)
        self.cantrips = kwargs.pop("cantrips", None)
        self.resources = kwargs.pop("resources", None)
        self.conditions = kwargs.pop("conditions", None)
        self.extras = kwargs.pop("extras", None)
        self.classes = kwargs.pop("classes", None)
        for k, v in kwargs.items():
            setattr(self, k, v)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.updated_at = now
        self.created_at = now


def wizard_sheet(**overrides):
    base = dict(
        spells=[{"name": "Fireball", "prepared": True}, {"name": "Magic Missile", "prepared": True}, "Shield"],
        cantrips=["Fire Bolt", {"name": "Sacred Flame"}],
        spell_slots={"1": {"max": 4, "used": 0}, "2": {"max": 3, "used": 0}, "3": {"max": 2, "used": 0}},
    )
    base.update(overrides)
    return FakeSheet(**base)


# ── Known / prepared queries ──────────────────────────────────────────────


def test_query_known_spells_normalizes_sheet_data():
    known = query_known_spells(wizard_sheet())
    assert "fire bolt" in known.cantrips
    assert "sacred flame" in known.cantrips
    assert "fireball" in known.spells
    assert "shield" in known.spells  # string shorthand counts as castable
    assert "fireball" in known.prepared
    assert known.lists_present is True


def test_query_known_spells_unprepared_excluded_from_prepared():
    known = query_known_spells(wizard_sheet(spells=[{"name": "Fireball", "prepared": False}]))
    assert "fireball" in known.spells
    assert "fireball" not in known.prepared


def test_query_known_spells_malformed_list_fails_closed():
    with pytest.raises(SpellError) as exc:
        query_known_spells(wizard_sheet(spells="fireball"))
    assert exc.value.code == "malformed_spell_list"


# ── Validation: eligibility ───────────────────────────────────────────────


def test_validate_cantrip_needs_no_slot():
    v = validate_spell_cast(wizard_sheet(), "Fire Bolt")
    assert v.valid is True
    assert v.cast_slot_level is None
    assert v.concentration_op == "none"
    assert v.rule_ref["supported"] == "true"
    assert "SRD" in v.rule_ref["citation"]


def test_validate_leveled_spell_defaults_to_base_slot():
    v = validate_spell_cast(wizard_sheet(), "Magic Missile")
    assert v.valid is True
    assert v.cast_slot_level == 1


def test_validate_unknown_spell_rejected_before_staging():
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(wizard_sheet(), "Hold Person")
    # Hold Person is supported but not on this sheet's list — rejected pre-staging
    assert exc.value.code == "spell_not_known"


def test_validate_spell_not_on_list_rejected():
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(wizard_sheet(), "Hold Person")
    assert exc.value.code == "spell_not_known"


def test_validate_explicitly_unprepared_spell_rejected():
    sheet = wizard_sheet(spells=[{"name": "Fireball", "prepared": False}])
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(sheet, "Fireball")
    assert exc.value.code == "spell_not_prepared"


def test_validate_missing_spell_lists_fails_closed():
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(FakeSheet(spells=None, cantrips=None), "Fire Bolt")
    assert exc.value.code == "spell_not_known"


def test_validate_missing_casting_ability_rejected():
    sheet = wizard_sheet()
    sheet.spellcasting_ability = None
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(sheet, "Fire Bolt")
    assert exc.value.code == "no_spellcasting_ability"


def test_validate_reaction_requires_reaction():
    sheet = wizard_sheet()  # Shield is on the list via shorthand
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(sheet, "Shield", actions=ActionEconomy(has_reaction=False))
    assert exc.value.code == "reaction_required"
    v = validate_spell_cast(sheet, "Shield", actions=ActionEconomy(has_reaction=True))
    assert v.valid is True and v.cast_slot_level == 1


def test_validate_incapacitated_cannot_cast():
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(wizard_sheet(), "Fire Bolt", actions=ActionEconomy(incapacitated=True))
    assert exc.value.code == "cannot_act"


def test_validate_insufficient_slot():
    sheet = wizard_sheet(spell_slots={"1": {"max": 1, "used": 1}})
    with pytest.raises(SpellError) as exc:
        validate_spell_cast(sheet, "Magic Missile")
    assert exc.value.code == "insufficient_slot"


def test_validate_concentration_replace_vs_start():
    cleric = FakeSheet(
        spells=["Bless"], spell_slots={"1": {"max": 4, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
    )
    v = validate_spell_cast(cleric, "Bless")
    assert v.valid is True and v.concentration_op == "start"
    concentrating = FakeSheet(
        spells=["Bless"], spell_slots={"1": {"max": 4, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
        extras={"concentration": {"active": True, "effect_name": "Hex", "effect_id": "x", "source": "spell:hex"}},
    )
    v2 = validate_spell_cast(concentrating, "Bless")
    assert v2.valid is True and v2.concentration_op == "replace"
    assert v2.concentration_broke == "Hex"


# ── Fallback: unsupported long tail ───────────────────────────────────────


def test_unsupported_spell_falls_back_without_consuming():
    v = validate_spell_cast(wizard_sheet(), "Wish")
    assert v.valid is False
    assert v.failure_code == "unsupported_spell"
    assert v.fallback is not None
    assert v.fallback["consumes_resources"] is False
    assert "retrieval_query" in v.fallback
    ref = spell_rule_ref("Wish")
    assert ref["supported"] == "false"


def test_is_spell_supported_and_catalog_refs():
    assert is_spell_supported("Fireball") is True
    assert is_spell_supported("Wish") is False
    ref = spell_rule_ref("Fireball")
    assert ref["rule_id"] == "srd521.spells.fireball"
    assert ref["supported"] == "true"
    assert get_spell_def("fireball").save_ability == "dexterity"


# ── Expressions: cantrip scaling + upcasting ──────────────────────────────


def test_cantrip_scaling_and_upcast_expressions():
    assert cantrip_dice_count(1) == 1
    assert cantrip_dice_count(5) == 2
    assert cantrip_dice_count(11) == 3
    assert cantrip_dice_count(17) == 4
    assert damage_expression_for_slot(get_spell_def("Fire Bolt"), None, character_level=5) == "2d10"
    assert damage_expression_for_slot(get_spell_def("Fireball"), 3) == "8d6"
    assert damage_expression_for_slot(get_spell_def("Fireball"), 4) == "9d6"
    assert healing_expression_for_slot(get_spell_def("Cure Wounds"), 1, ability_modifier=3) == "2d8+3"
    assert healing_expression_for_slot(get_spell_def("Cure Wounds"), 2, ability_modifier=3) == "4d8+3"
    assert healing_expression_for_slot(get_spell_def("Healing Word"), 1, ability_modifier=3) == "2d4+3"


# ── Staging: slot spend + concentration, idempotent ───────────────────────


def test_stage_cantrip_has_no_slot_spend():
    v = validate_spell_cast(wizard_sheet(), "Fire Bolt")
    staged = stage_spell_cast(v, cast_id="cast-1", caster_kind="pc", caster_id=str(uuid.uuid4()))
    assert staged.staged_effects == []
    assert staged.resolution_plan["needs_attack_roll"] is True


def test_stage_leveled_spell_spends_slot_and_concentration():
    cleric = FakeSheet(
        spells=["Bless"], spell_slots={"1": {"max": 4, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
    )
    v = validate_spell_cast(cleric, "Bless")
    caster_id = str(uuid.uuid4())
    staged = stage_spell_cast(v, cast_id="cast-2", caster_kind="pc", caster_id=caster_id)
    types = [e["effect_type"] for e in staged.staged_effects]
    assert "apply_resource" in types
    assert "apply_concentration" in types
    slot_eff = next(e for e in staged.staged_effects if e["effect_type"] == "apply_resource")
    assert slot_eff["arguments"]["resource"] is None
    assert slot_eff["arguments"]["slot_level"] == 1
    assert slot_eff["arguments"]["target_id"] == caster_id


def test_stage_leveled_concentration_cast_passes_duplicate_mutation_preflight():
    # Bless stages a slot spend AND a concentration start: the canonical #227
    # preflight rejects two state effects sharing one mutation ID, so the
    # staged pair must carry distinct stable mutation IDs.
    from app.dm.effects import _reject_duplicate_state_mutations

    cleric = FakeSheet(
        spells=["Bless"], spell_slots={"1": {"max": 4, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
    )
    v = validate_spell_cast(cleric, "Bless")
    staged = stage_spell_cast(v, cast_id="cast-mut", caster_kind="pc", caster_id=str(uuid.uuid4()))
    assert len(staged.staged_effects) == 2
    mutation_ids = [e["arguments"]["mutation_id"] for e in staged.staged_effects]
    assert len(set(mutation_ids)) == 2
    _reject_duplicate_state_mutations(staged.staged_effects)  # must not raise
    # Duplicate retry stages identical records (idempotent replay).
    retry = stage_spell_cast(v, cast_id="cast-mut", caster_kind="pc", caster_id=staged.staged_effects[0]["arguments"]["target_id"])
    assert retry.staged_effects == staged.staged_effects


def test_stage_invalid_validation_rejected_before_commit():
    v = validate_spell_cast(wizard_sheet(), "Wish")
    with pytest.raises(SpellError):
        stage_spell_cast(v, cast_id="cast-3", caster_kind="pc", caster_id=str(uuid.uuid4()))


def test_duplicate_retry_stages_identical_effects():
    sheet = wizard_sheet()
    v = validate_spell_cast(sheet, "Magic Missile")
    caster_id = str(uuid.uuid4())
    first = stage_spell_cast(v, cast_id="cast-dup", caster_kind="pc", caster_id=caster_id)
    second = stage_spell_cast(v, cast_id="cast-dup", caster_kind="pc", caster_id=caster_id)
    assert first.staged_effects == second.staged_effects
    ledger = SpellLedger()
    assert ledger.apply("cast-dup") == "applied"
    assert ledger.apply("cast-dup") == "duplicate"


def test_durable_duplicate_retry_replays(monkeypatch):
    from app.rules.spells import _require_cast_id  # noqa

    records: dict[str, dict] = []

    class FakeDB:
        pass

    import app.rules.spells as spells_mod

    real = spells_mod.apply_spell_consequence

    def fake_apply(db, *, actor_id, scope_type, scope_id, cast_id, payload, execute, command_type="spellcasting.apply"):
        for entry in records:
            if entry["key"] == cast_id and entry["scope"] == str(scope_id):
                if entry["payload"] != payload:
                    from app.idempotency import IdempotencyConflictError

                    raise IdempotencyConflictError("conflict")
                return entry["result"], True
        result = execute()
        records.append({"key": cast_id, "scope": str(scope_id), "payload": payload, "result": result})
        return result, False

    monkeypatch.setattr(spells_mod, "apply_spell_consequence", fake_apply)
    try:
        db = FakeDB()
        payload = {"spell": "magic_missile", "slot": 1}
        r1, replayed1 = spells_mod.apply_spell_consequence(
            db, actor_id="a", scope_type="campaign", scope_id="s",
            cast_id="cast-d1", payload=payload, execute=lambda: {"spent": 1},
        )
        r2, replayed2 = spells_mod.apply_spell_consequence(
            db, actor_id="a", scope_type="campaign", scope_id="s",
            cast_id="cast-d1", payload=payload, execute=lambda: {"spent": 1},
        )
        assert (r1, replayed1) == ({"spent": 1}, False)
        assert (r2, replayed2) == ({"spent": 1}, True)
    finally:
        monkeypatch.setattr(spells_mod, "apply_spell_consequence", real)


# ── Resolution: attack spell ──────────────────────────────────────────────


def test_spell_attack_resolves_via_226_primitives():
    caster = wizard_sheet()
    target = FakeSheet(armor_class=10)
    defender = defender_from_sheet(target)
    assert spell_attacker(caster, "Fire Bolt").attack_bonus == 5  # +3 int, +2 prof
    attack = resolve_spell_attack(
        caster=caster, spell_name="Fire Bolt", defender=defender, dice=[15], attack_id="atk-1",
    )
    assert attack.outcome in ("hit", "critical")
    damage = resolve_spell_damage(
        spell_name="Fire Bolt", slot_level=None, damage_rolls=[7], damage_id="dmg-1",
    )
    assert damage.final_total == 7
    effect = build_spell_damage_effect(
        effect_id="eff-1", target_kind="npc", target_id=str(uuid.uuid4()),
        damage=damage, attack=attack,
    )
    assert effect["effect_type"] == "apply_attack_damage"
    assert effect["arguments"]["damage_total"] == 7


# ── Resolution: save spell ────────────────────────────────────────────────


def test_save_spell_resolves_via_225_primitive():
    caster = wizard_sheet()  # int 16, prof 2 → DC 13
    assert spell_save_dc(caster, "Fireball") == 13
    target = FakeSheet(dexterity=10)  # +0 dex save
    result = resolve_spell_save(
        caster=caster, spell_name="Fireball", target=target, dice=[2], roll_id="save-1",
    )
    assert result.success is False
    assert result.dc == 13
    saved = resolve_spell_save(
        caster=caster, spell_name="Fireball", target=target, dice=[20], roll_id="save-2",
    )
    assert saved.success is True  # natural 20 always succeeds


def test_save_spell_pc_cast_at_npc_uses_npc_roller_path():
    caster = wizard_sheet()  # int 16, prof 2 → DC 13 (public)
    npc_target = FakeSheet(dexterity=10)  # +0 dex save
    failed = resolve_spell_save(
        caster=caster, spell_name="Fireball", target=npc_target,
        target_kind="npc", dice=[2], roll_id="save-npc-1",
    )
    assert failed.success is False
    assert failed.provenance["roller"] == "npc"
    assert failed.die_source == "dm_supplied"
    assert failed.die_visibility == "hidden"
    assert "dice_all" not in failed.public_projection()  # NPC dice stay DM-private
    assert failed.public_projection()["dc"] == 13  # PC caster DC is public
    # NPC saves never require player-supplied dice: runtime generation works.
    generated = resolve_spell_save(
        caster=caster, spell_name="Fireball", target=npc_target,
        target_kind="npc", roll_id="save-npc-2",
    )
    assert generated.die_source == "runtime_generated"
    assert generated.success in (True, False)


def test_save_spell_npc_cast_at_pc_hides_dc():
    npc_caster = {"spell_save_dc": 14, "spell_attack_bonus": 6}
    pc_target = FakeSheet(dexterity=10)  # +0 dex save
    result = resolve_spell_save(
        caster=npc_caster, spell_name="Fireball", target=pc_target,
        caster_kind="npc", target_kind="pc", dice=[12], roll_id="save-pcnpc-1",
    )
    assert result.success is False  # 12 < hidden DC 14
    assert result.dc == 14
    assert result.dc_visibility == "hidden"
    assert "dc" not in result.public_projection()  # NPC DC stays DM-private
    assert result.die_visibility == "public"  # PC roller dice stay observable
    assert result.public_projection()["die_kept"] == 12


# ── Resolution: damage + healing ──────────────────────────────────────────


def test_spell_damage_upcast_and_healing_hp_change():
    damage = resolve_spell_damage(
        spell_name="Fireball", slot_level=4,
        damage_rolls=[6] * 9, damage_id="dmg-fb",
    )
    assert damage.final_total == 54
    assert damage.damage_type == "fire"

    from app.rules.attacks import HitPoints

    caster = FakeSheet(spellcasting_ability="wisdom", wisdom=16)  # +3
    receipt, change = resolve_spell_healing(
        spell_name="Cure Wounds", slot_level=1, heal_rolls=[6, 5],
        ability_modifier=3, current_hp=HitPoints(current=4, maximum=10, temporary=0),
        damage_id="heal-1", change_id="chg-1",
    )
    assert receipt.final_total == 14  # 6 + 5 + 3 (SRD 5.2.1: 2d8 + mod)
    assert change.after.current == 10  # capped at max
    assert change.kind == "heal"
    # Healing has no staged commit shape in #228: it resolves purely via
    # resolve_spell_healing; the staged-effect form arrives with the later
    # integration lane that registers its contract/registry handler.


# ── Resolution: concentration + condition ─────────────────────────────────


def test_concentration_and_failed_save_condition():
    cleric = FakeSheet(
        spells=["Hold Person"], spell_slots={"2": {"max": 3, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
    )
    v = validate_spell_cast(cleric, "Hold Person", slot_level=2)
    assert v.concentration_op == "start"
    staged = stage_spell_cast(v, cast_id="cast-hp", caster_kind="pc", caster_id=str(uuid.uuid4()))
    assert any(e["effect_type"] == "apply_concentration" for e in staged.staged_effects)
    assert staged.resolution_plan["applies_condition_on_failed_save"] == "paralyzed"

    target_id = str(uuid.uuid4())
    cond = build_spell_condition_effect(
        effect_id="cond-1", mutation_id="mut-1", target_kind="npc", target_id=target_id,
        spell_name="Hold Person", save_failed=True, save_dc=13, duration_rounds=10,
    )
    assert cond is not None
    assert cond["effect_type"] == "apply_condition"
    assert cond["arguments"]["condition"] == "paralyzed"
    assert build_spell_condition_effect(
        effect_id="cond-2", mutation_id="mut-2", target_kind="npc", target_id=target_id,
        spell_name="Hold Person", save_failed=False,
    ) is None


# ── Hidden NPC spells ─────────────────────────────────────────────────────


def test_hidden_npc_spell_mechanically_usable_but_private():
    npc_id = str(uuid.uuid4())
    details = {
        "spells": ["Fireball"],
        "cantrips": ["Fire Bolt"],
        "spell_slots": {"3": {"max": 2, "used": 0}},
        "spellcasting_ability": "intelligence",
        "spell_save_dc": 14,
        "spell_attack_bonus": 6,
        "visibility": "dm_only",
    }
    known = query_npc_spells(details)
    assert "fireball" in known.spells
    v = validate_spell_cast(details, "Fireball", caster_kind="npc", slot_level=3)
    assert v.valid is True
    assert spell_save_dc(details, "Fireball", caster_kind="npc") == 14
    staged = stage_spell_cast(v, cast_id="cast-npc", caster_kind="npc", caster_id=npc_id)
    assert staged.npc_private is True

    projected = project_npc_spells_for_viewer(details, is_authority=False)
    assert projected["spells"] == []
    assert projected["spell_slots"] == {}
    authority = project_npc_spells_for_viewer(details, is_authority=True)
    assert authority["spells"] == ["Fireball"]

    event_type, payload, visibility = spell_domain_event(staged, include_private=False)
    assert visibility == "dm_private"
    assert payload["redacted"] is True
    _, public_payload, public_vis = spell_domain_event(
        stage_spell_cast(
            validate_spell_cast(wizard_sheet(), "Fire Bolt"),
            cast_id="cast-pc1", caster_kind="pc", caster_id=str(uuid.uuid4()),
        ),
        include_private=False,
    )
    assert public_vis == "public"
    assert public_payload["spell_name"] == "Fire Bolt"


# ── Staged effects survive the canonical DM contract ───────────────────────


def test_spell_staged_effects_survive_canonical_contract_validation():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    cleric = FakeSheet(
        spells=["Hold Person"], spell_slots={"2": {"max": 3, "used": 0}},
        spellcasting_ability="wisdom", wisdom=16,
    )
    caster_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    staged = stage_spell_cast(
        validate_spell_cast(cleric, "Hold Person", slot_level=2),
        cast_id="cast-ctr", caster_kind="pc", caster_id=caster_id,
    )
    cond = build_spell_condition_effect(
        effect_id="ctr-spellcond", mutation_id="ctr-mut", target_kind="pc",
        target_id=target_id, spell_name="Hold Person",
        save_failed=True, save_dc=13, duration_rounds=10,
    )
    claim = {
        "text": "The wizard gestures.",
        "claim_kind": "observation",
        "origin": "dm_adjudication",
        "visibility": "public",
    }
    c = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "spellcasting",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [claim]}],
            "staged_effects": [*staged.staged_effects, cond],
        }
    )
    assert [e.effect_type for e in c.staged_effects] == [
        "apply_resource", "apply_concentration", "apply_condition",
    ]


def test_provider_schema_excludes_code_built_spell_effects():
    # Spell casts stage only code-built effect types (slot spend, damage,
    # condition, concentration): none may be provider-authorable, and there
    # is no provider spell-cast effect path that could fake support.
    from app.dm.contract import contract_json_schema_strict

    schema = contract_json_schema_strict()
    effect_type_enum = schema["$defs"]["StagedEffect"]["properties"]["effect_type"]["enum"]
    for code_built in ("apply_attack_damage", "apply_condition", "apply_resource", "apply_concentration"):
        assert code_built not in effect_type_enum
    assert not any("spell" in str(t).lower() for t in effect_type_enum)
