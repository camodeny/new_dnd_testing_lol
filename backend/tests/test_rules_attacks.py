"""Unit fixtures for #226 — deterministic attack, damage, HP, temp-HP resolution."""

import datetime
import random
import uuid

import pytest

from app.rules.attacks import (
    AttackError,
    AttackLedger,
    DamageContribution,
    HitPoints,
    apply_attack_consequence,
    apply_damage,
    attacker_from_npc,
    attacker_from_sheet,
    attack_domain_event,
    build_damage_effect,
    damage_domain_event,
    defender_from_npc,
    defender_from_sheet,
    grant_temporary_hp,
    heal_damage,
    hp_from_npc,
    hp_from_sheet,
    make_damage_spec,
    parse_damage_expression,
    resolve_attack_roll,
    resolve_damage,
    resolve_full_attack,
    runtime_damage_dice,
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
            "acrobatics",
            "animal_handling",
            "arcana",
            "athletics",
            "deception",
            "history",
            "insight",
            "intimidation",
            "investigation",
            "medicine",
            "nature",
            "perception",
            "performance",
            "persuasion",
            "religion",
            "sleight_of_hand",
            "stealth",
            "survival",
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
        self.classes = kwargs.pop("classes", None)
        for k, v in kwargs.items():
            setattr(self, k, v)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.updated_at = now
        self.created_at = now


def pc_attacker(name="Longsword", bonus=5):
    sheet = FakeSheet(
        weapons=[
            {
                "name": name,
                "attack_bonus": bonus,
                "damage": "1d8+3",
                "damage_type": "slashing",
            }
        ]
    )
    return attacker_from_sheet(sheet, name)


def npc_defender(ac=15):
    return defender_from_npc(armor_class=ac)


# ── Hit / miss / critical ─────────────────────────────────────────────────


def test_hit_miss_meet_or_beat_deterministic():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    first = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="public",
        attack_id="atk-hit",
    )
    second = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="public",
        attack_id="atk-hit",
    )
    assert first.outcome == "hit"
    assert first.is_critical is False
    assert first.total == 17  # 12 + 5
    assert first.calculation_path == "attack_authoritative"
    assert first.model_dump() == second.model_dump()  # pure retry is identical

    miss = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[5],
        ac_visibility="public",
        attack_id="atk-miss",
    )
    assert miss.outcome == "miss"
    assert miss.total == 10

    meet = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[10],
        ac_visibility="public",
        attack_id="atk-meet",
    )
    assert meet.total == 15
    assert meet.outcome == "hit"  # meet-or-beat


def test_natural_20_critical_always_hits():
    attacker = pc_attacker(bonus=0)
    defender = npc_defender(ac=30)  # unreachable by arithmetic
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[20],
        ac_visibility="public",
        attack_id="atk-crit",
    )
    assert res.is_natural_20 is True
    assert res.is_critical is True
    assert res.outcome == "critical"


def test_natural_1_always_misses():
    attacker = pc_attacker(bonus=10)
    defender = npc_defender(ac=5)  # trivially reachable, still misses
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[1],
        ac_visibility="public",
        attack_id="atk-fumble",
    )
    assert res.is_natural_1 is True
    assert res.is_critical is False
    assert res.outcome == "miss"


def test_attack_advantage_keeps_higher():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[4, 12],
        advantage_state="advantage",
        ac_visibility="public",
        attack_id="atk-adv",
    )
    assert res.die_kept == 12
    assert res.dice_dropped == [4]
    assert res.outcome == "hit"


def test_attack_advantage_sources_cancel():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        advantage_sources=["advantage", "disadvantage"],
        ac_visibility="public",
        attack_id="atk-cancel",
    )
    assert res.advantage_state == "normal"
    assert res.outcome == "hit"


def test_attack_id_is_required_never_minted():
    attacker = pc_attacker()
    defender = npc_defender()
    for bad in (None, "", "   "):
        with pytest.raises(AttackError) as exc:
            resolve_attack_roll(
                attacker=attacker,
                defender=defender,
                attacker_kind="pc",
                dice=[10],
                attack_id=bad,
            )
        assert exc.value.code in ("missing_roll_id", "missing_effect_id")
        assert exc.value.field == "attack_id"


# ── Damage: critical dice doubling, mitigation hooks ──────────────────────


def test_normal_damage_applies_modifier_once():
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3, damage_type="slashing")
    res = resolve_damage(
        spec=spec,
        damage_rolls=[6],
        attacker_kind="pc",
        is_critical=False,
        damage_id="dmg-normal",
    )
    assert res.pre_mitigation == 9
    assert res.final_total == 9
    assert res.mitigation == "none"
    assert res.dice_expected == 1


def test_critical_damage_doubles_dice_not_modifier():
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3, damage_type="slashing")
    with pytest.raises(AttackError) as exc:
        resolve_damage(
            spec=spec,
            damage_rolls=[6],
            attacker_kind="pc",
            is_critical=True,
            damage_id="dmg-crit-short",
        )
    assert exc.value.code == "invalid_dice_count"
    res = resolve_damage(
        spec=spec,
        damage_rolls=[6, 4],
        attacker_kind="pc",
        is_critical=True,
        damage_id="dmg-crit",
    )
    assert res.dice_expected == 2
    assert res.pre_mitigation == 13  # 6 + 4 + 3 (modifier once)
    assert res.final_total == 13


def test_damage_mitigation_resistance_vulnerability_immunity():
    spec = make_damage_spec(
        num_dice=1, die_size=8, modifier=2, damage_type="fire"
    )  # 5+2=7
    resisted = defender_from_npc(armor_class=10, resistances=["fire"])
    res = resolve_damage(
        spec=spec,
        damage_rolls=[5],
        attacker_kind="pc",
        defender=resisted,
        damage_id="dmg-res",
    )
    assert res.mitigation == "resistance"
    assert res.final_total == 3  # 7 // 2

    vulnerable = defender_from_npc(armor_class=10, vulnerabilities=["fire"])
    res = resolve_damage(
        spec=spec,
        damage_rolls=[5],
        attacker_kind="pc",
        defender=vulnerable,
        damage_id="dmg-vuln",
    )
    assert res.mitigation == "vulnerability"
    assert res.final_total == 14

    immune = defender_from_npc(armor_class=10, immunities=["fire"])
    res = resolve_damage(
        spec=spec,
        damage_rolls=[5],
        attacker_kind="pc",
        defender=immune,
        damage_id="dmg-imm",
    )
    assert res.mitigation == "immunity"
    assert res.final_total == 0


def test_damage_hooks_and_extra_modifiers():
    spec = make_damage_spec(num_dice=1, die_size=6, modifier=1, damage_type="piercing")

    def hunter_mark(ctx):
        return 2

    res = resolve_damage(
        spec=spec,
        damage_rolls=[4],
        attacker_kind="pc",
        extra_modifiers=[DamageContribution(name="sneak", value=3)],
        hooks=[hunter_mark],
        damage_id="dmg-hooks",
    )
    assert res.pre_mitigation == 10  # 4 + 1 + 3 + 2
    assert "extra:sneak" in res.damage_components
    assert "hook:hunter_mark" in res.damage_components
    assert res.provenance["hook_names"] == ["hunter_mark"]


def test_damage_expression_parsing():
    spec = parse_damage_expression("2d6+3", damage_type="Fire")
    assert (spec.num_dice, spec.die_size, spec.modifier, spec.damage_type) == (
        2,
        6,
        3,
        "fire",
    )
    assert parse_damage_expression("1d8").modifier == 0
    with pytest.raises(AttackError) as exc:
        parse_damage_expression("banana")
    assert exc.value.code == "invalid_damage_expression"
    with pytest.raises(AttackError):
        make_damage_spec(num_dice=1, die_size=7)


# ── HP / temp-HP primitives ───────────────────────────────────────────────


def test_temp_hp_absorbs_first():
    hp = HitPoints(current=20, maximum=30, temporary=8)
    change = apply_damage(hp, 10, change_id="hp-temp-1")
    assert change.absorbed_by_temp == 8
    assert change.applied_to_current == 2
    assert change.after.temporary == 0
    assert change.after.current == 18
    assert change.is_down is False


def test_current_hp_damage_floors_at_zero():
    hp = HitPoints(current=5, maximum=30, temporary=0)
    change = apply_damage(hp, 50, change_id="hp-floor")
    assert change.applied_to_current == 5
    assert change.after.current == 0
    assert change.is_down is True


def test_temp_grant_never_stacks_higher_wins():
    hp = HitPoints(current=20, maximum=30, temporary=8)
    up = grant_temporary_hp(hp, 12, change_id="tmp-up")
    assert up.after.temporary == 12
    assert up.temp_granted == 4
    down = grant_temporary_hp(hp, 3, change_id="tmp-down")
    assert down.after.temporary == 8  # lower grant ignored
    assert down.temp_granted == 0


def test_heal_capped_at_maximum():
    hp = HitPoints(current=25, maximum=30, temporary=4)
    change = heal_damage(hp, 10, change_id="heal-cap")
    assert change.restored_to_current == 5
    assert change.after.current == 30
    assert change.after.temporary == 4  # temp untouched


# ── Player vs NPC roll ownership ──────────────────────────────────────────


def test_pc_attack_and_damage_require_supplied_dice():
    attacker = pc_attacker()
    defender = npc_defender()
    with pytest.raises(AttackError) as exc:
        resolve_attack_roll(
            attacker=attacker,
            defender=defender,
            attacker_kind="pc",
            dice=None,
            attack_id="pc-nodie",
        )
    assert exc.value.code == "missing_player_die"
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=1)
    with pytest.raises(AttackError) as exc:
        resolve_damage(
            spec=spec, damage_rolls=None, attacker_kind="pc", damage_id="pc-nodmg"
        )
    assert exc.value.code == "missing_player_die"


def test_npc_dm_supplied_and_runtime_generated():
    attacker = attacker_from_npc(attack_bonus=4, attack_name="Claw")
    defender = defender_from_npc(armor_class=13)
    supplied = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[15],
        attack_id="npc-supplied",
    )
    assert supplied.die_source == "dm_supplied"
    assert supplied.total == 19
    assert supplied.outcome == "hit"

    seeded = random.Random(99)
    generated = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=None,
        attack_id="npc-gen",
        rng=seeded,
    )
    assert generated.die_source == "runtime_generated"
    again = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=None,
        attack_id="npc-gen",
        rng=random.Random(99),
    )
    assert again.die_kept == generated.die_kept  # seeded runtime dice reproduce

    spec = make_damage_spec(num_dice=2, die_size=6, modifier=2)
    dmg = resolve_damage(
        spec=spec,
        damage_rolls=None,
        attacker_kind="npc",
        damage_id="npc-dmg",
        rng=random.Random(7),
    )
    assert dmg.die_source == "runtime_generated"
    assert len(dmg.dice_rolled) == 2
    assert dmg.pre_mitigation == sum(dmg.dice_rolled) + 2


def test_runtime_damage_dice_validation():
    assert len(runtime_damage_dice(num_dice=3, die_size=6, rng=random.Random(0))) == 3
    with pytest.raises(AttackError):
        runtime_damage_dice(num_dice=0, die_size=6, rng=random.Random(0))


# ── Hidden AC privacy ─────────────────────────────────────────────────────


def test_hidden_ac_absent_from_public_projection():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="hidden",
        attack_id="atk-hidden",
    )
    assert res.armor_class == 15
    public = res.public_projection()
    assert "armor_class" not in public
    assert public["outcome"] == "hit"  # observable outcome stays
    assert public["total"] == 17  # PC dice are public
    full = res.to_event_payload(include_private=True)
    assert full["armor_class"] == 15

    open_res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="public",
        attack_id="atk-open",
    )
    assert open_res.public_projection()["armor_class"] == 15


def test_hidden_npc_roll_hides_dice_and_total():
    attacker = attacker_from_npc(attack_bonus=4)
    defender = defender_from_npc(armor_class=13)
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[15],
        die_visibility="hidden",
        attack_id="npc-hidden",
    )
    public = res.public_projection()
    assert "die_kept" not in public
    assert "total" not in public
    assert "dice_all" not in public
    assert "armor_class" not in public  # default hidden
    assert public["outcome"] == "hit"
    assert res.to_event_payload(include_private=False) == public


# ── Missing-stat failures (fail closed, never guessed) ────────────────────


def test_missing_stats_fail_closed():
    sheet_bare = FakeSheet()  # no weapons/attacks
    with pytest.raises(AttackError) as exc:
        attacker_from_sheet(sheet_bare)
    assert exc.value.code == "missing_stat"

    sheet_multi = FakeSheet(
        weapons=[
            {"name": "Axe", "attack_bonus": 5},
            {"name": "Bow", "attack_bonus": 4},
        ]
    )
    with pytest.raises(AttackError) as exc:
        attacker_from_sheet(sheet_multi)  # ambiguous without a name
    assert exc.value.code == "ambiguous_attack"
    with pytest.raises(AttackError) as exc:
        attacker_from_sheet(sheet_multi, "Missing Blade")
    assert exc.value.code == "missing_stat"

    sheet_no_bonus = FakeSheet(weapons=[{"name": "Stick"}])  # no attack_bonus
    with pytest.raises(AttackError) as exc:
        attacker_from_sheet(sheet_no_bonus, "Stick")
    assert exc.value.code == "missing_stat"

    with pytest.raises(AttackError) as exc:
        attacker_from_npc()  # no explicit bonus, no details
    assert exc.value.code == "missing_stat"
    assert exc.value.field == "attack_bonus"

    with pytest.raises(AttackError) as exc:
        defender_from_npc()
    assert exc.value.code == "missing_stat"
    assert exc.value.field == "armor_class"

    with pytest.raises(AttackError) as exc:
        hp_from_npc(current=10)  # maximum missing
    assert exc.value.code == "missing_stat"


def test_invalid_sheet_blocks_attack_inputs():
    bad = FakeSheet(strength=99)
    with pytest.raises(AttackError) as exc:
        attacker_from_sheet(
            FakeSheet(strength=99, weapons=[{"name": "Sword", "attack_bonus": 5}]),
            "Sword",
        )
    assert exc.value.code == "invalid_ability_score"
    with pytest.raises(AttackError):
        defender_from_sheet(bad)
    with pytest.raises(AttackError):
        hp_from_sheet(bad)


def test_npc_details_fallback_and_sheet_queries():
    attacker = attacker_from_npc(
        details={"attack_bonus": 6, "attack_name": "Bite", "damage": "1d6+2"}
    )
    assert attacker.attack_bonus == 6
    assert attacker.attack_name == "Bite"
    assert attacker.damage_expression == "1d6+2"

    defender = defender_from_npc(details={"ac": 13, "resistances": ["Cold"]})
    assert defender.armor_class == 13
    assert defender.resistances == ["cold"]

    hp = hp_from_npc(
        details={"hit_points": {"current": 22, "maximum": 30, "temporary": 5}}
    )
    assert (hp.current, hp.maximum, hp.temporary) == (22, 30, 5)

    sheet = FakeSheet(
        armor_class=16,
        hit_points_max=24,
        hit_points_current=20,
        hit_points_temp=3,
        weapons=[{"name": "Mace", "attack_bonus": 4}],
    )
    assert defender_from_sheet(sheet).armor_class == 16
    assert defender_from_sheet(sheet).calculation_path == "attack_authoritative"
    assert hp_from_sheet(sheet).model_dump() == {
        "current": 20,
        "maximum": 24,
        "temporary": 3,
    }
    assert (
        attacker_from_sheet(sheet, "mace").attack_bonus == 4
    )  # case-insensitive name match


# ── Full attack flow ──────────────────────────────────────────────────────


def test_full_attack_hit_applies_damage_through_temp():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    hp = HitPoints(current=20, maximum=30, temporary=8)
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3, damage_type="slashing")
    result = resolve_full_attack(
        attacker=attacker,
        defender=defender,
        defender_hp=hp,
        attacker_kind="pc",
        attack_dice=[12],
        damage_spec=spec,
        damage_rolls=[6],
        ac_visibility="public",
        attack_id="full-hit",
        damage_id="full-dmg",
        change_id="full-hp",
    )
    assert result.attack.outcome == "hit"
    assert result.damage is not None and result.damage.final_total == 9
    assert result.hp_change is not None
    assert result.hp_change.absorbed_by_temp == 8
    assert result.hp_change.after.current == 19


def test_full_attack_miss_resolves_no_damage():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    hp = HitPoints(current=20, maximum=30, temporary=8)
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
    result = resolve_full_attack(
        attacker=attacker,
        defender=defender,
        defender_hp=hp,
        attacker_kind="pc",
        attack_dice=[5],
        damage_spec=spec,
        damage_rolls=[6],
        attack_id="full-miss",
    )
    assert result.attack.outcome == "miss"
    assert result.damage is None  # no damage invented for a miss
    assert result.hp_change is None


def test_full_attack_hit_without_spec_leaves_damage_unresolved():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=10)
    result = resolve_full_attack(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        attack_dice=[12],
        damage_spec=None,
        attack_id="full-nospec",
    )
    assert result.attack.outcome == "hit"
    assert result.damage is None


def test_full_attack_critical_doubles_supplied_damage_dice():
    attacker = attacker_from_npc(attack_bonus=4)
    defender = defender_from_npc(armor_class=10)
    spec = make_damage_spec(num_dice=1, die_size=6, modifier=1)
    result = resolve_full_attack(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        attack_dice=[20],
        damage_spec=spec,
        damage_rolls=[3, 5],
        attack_id="full-crit",
        damage_id="full-crit-dmg",
    )
    assert result.attack.outcome == "critical"
    assert result.damage is not None
    assert result.damage.pre_mitigation == 9  # 3 + 5 + 1


# ── Duplicate application guard ───────────────────────────────────────────


def test_attack_ledger_cannot_apply_twice():
    ledger = AttackLedger()
    assert ledger.apply("atk-abc") == "applied"
    assert ledger.apply("atk-abc") == "duplicate"
    assert ledger.is_duplicate("atk-abc") is True
    assert ledger.is_duplicate("atk-other") is False
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


def test_durable_duplicate_damage_cannot_reduce_hp_twice():
    import uuid as _uuid
    from models.profiles import Profile

    factory = _sqlite_db()
    actor = _uuid.uuid4()
    with factory() as db:
        db.add(Profile(id=actor, email="attack-ledger@example.com"))
        db.commit()
    attacker = attacker_from_npc(attack_bonus=4)
    defender = defender_from_npc(armor_class=10)
    attack = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[15],
        attack_id="durable-atk-1",
    )
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=2)
    damage = resolve_damage(
        spec=spec,
        damage_rolls=[6],
        attacker_kind="npc",
        defender=defender,
        damage_id="durable-dmg-1",
        attack_id=attack.attack_id,
    )
    hp_change = apply_damage(
        HitPoints(current=20, maximum=20, temporary=5),
        damage.final_total,
        change_id="durable-hp-1",
    )
    calls: list[int] = []
    with factory() as db:
        first, replayed = apply_attack_consequence(
            db,
            actor_id=actor,
            scope_type="campaign",
            scope_id="camp-a",
            attack=attack,
            damage=damage,
            hp_change=hp_change,
            execute=lambda: calls.append(1)
            or {"hp_after": hp_change.after.model_dump(mode="json")},
        )
        assert replayed is False
    with factory() as restarted_db:  # new session = restart/worker boundary
        second, replayed_second = apply_attack_consequence(
            restarted_db,
            actor_id=actor,
            scope_type="campaign",
            scope_id="camp-a",
            attack=attack,
            damage=damage,
            hp_change=hp_change,
            execute=lambda: (_ for _ in ()).throw(
                AssertionError("must not re-execute on retry")
            ),
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
        db.add(Profile(id=actor, email="attack-conflict@example.com"))
        db.commit()
    attacker = attacker_from_npc(attack_bonus=4)
    defender = defender_from_npc(armor_class=10)
    first = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[15],
        attack_id="durable-atk-2",
    )
    altered = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[5],
        attack_id="durable-atk-2",
    )
    with factory() as db:
        apply_attack_consequence(
            db,
            actor_id=actor,
            scope_type="campaign",
            scope_id="camp-b",
            attack=first,
            execute=lambda: {"applied": True},
        )
        with pytest.raises(IdempotencyConflictError):
            apply_attack_consequence(
                db,
                actor_id=actor,
                scope_type="campaign",
                scope_id="camp-b",
                attack=altered,
                execute=lambda: {"applied": "tampered"},
            )


# ── Provenance / staged effects / domain events ───────────────────────────


def test_result_carries_audit_provenance():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    res = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        attack_id="audit-atk",
    )
    assert res.provenance["calculation_path"] == "attack_authoritative"
    assert res.provenance["mechanics_version"] == "mechanics_v1"
    assert res.provenance["rules_revision"] == "2024.5e"
    assert res.provenance["attack_version"] == "attack_v1"
    assert res.provenance["die_source"] == "player_supplied"
    assert res.attack_bonus_components == {"attack_bonus": 5}


def test_staged_damage_effect_record_shape():
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3, damage_type="slashing")
    damage = resolve_damage(
        spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="eff-dmg"
    )
    effect = build_damage_effect(
        effect_id="eff-1",
        target_kind="pc",
        target_id=str(uuid.uuid4()),
        damage=damage,
        visibility="public",
    )
    assert effect["effect_type"] == "apply_attack_damage"
    assert effect["arguments"]["damage_total"] == 9
    assert effect["arguments"]["damage_type"] == "slashing"
    assert effect["arguments"]["visibility"] == "public"
    with pytest.raises(AttackError):
        build_damage_effect(
            effect_id="eff-2",
            target_kind="pc",
            target_id=str(uuid.uuid4()),
            damage=damage,
            visibility="everyone",
        )
    with pytest.raises(AttackError):
        build_damage_effect(
            effect_id="eff-3",
            target_kind="dragon",
            target_id=str(uuid.uuid4()),
            damage=damage,
        )


def test_domain_event_builders():
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    attack = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="hidden",
        attack_id="evt-atk",
    )
    event_type, payload, visibility = attack_domain_event(attack, include_private=False)
    assert event_type == "attack.resolved"
    assert visibility == "dm_private"  # hidden AC stays restricted
    assert "armor_class" not in payload

    spec = make_damage_spec(num_dice=1, die_size=8, modifier=1)
    damage = resolve_damage(
        spec=spec,
        damage_rolls=[4],
        attacker_kind="pc",
        damage_id="evt-dmg",
        attack_id=attack.attack_id,
    )
    hp_change = apply_damage(
        HitPoints(current=10, maximum=10, temporary=0),
        damage.final_total,
        change_id="evt-hp",
    )
    event_type, payload, visibility = damage_domain_event(
        damage, hp_change, include_private=False
    )
    assert event_type == "damage.applied"
    assert payload["final_total"] == 5
    # Public HP payload is redacted: no exact snapshots, only the observable
    # outcome (kind + whether the target dropped).
    assert payload["hp_change"] == {
        "change_id": "evt-hp",
        "kind": "damage",
        "is_down": False,
    }
    assert "current" not in str(payload["hp_change"])
    assert "maximum" not in str(payload["hp_change"])
    # DM-private path still carries the full change for audit/replay.
    _, private_payload, _ = damage_domain_event(
        damage, hp_change, include_private=True
    )
    assert private_payload["hp_change"]["after"]["current"] == 5
    assert private_payload["hp_change"]["before"] == {
        "current": 10,
        "maximum": 10,
        "temporary": 0,
    }


def test_public_damage_event_never_exposes_hidden_npc_hp():
    """Hidden-NPC HP state stays DM-only on the public damage path (#226)."""
    import json

    attacker = attacker_from_npc(attack_bonus=4, attack_name="Claw")
    defender = defender_from_npc(armor_class=13)
    attack = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        dice=[15],
        attack_id="hide-hp-atk",
    )
    spec = make_damage_spec(num_dice=2, die_size=6, modifier=2)
    damage = resolve_damage(
        spec=spec,
        damage_rolls=[5, 5],
        attacker_kind="npc",
        defender=defender,
        die_visibility="hidden",
        damage_id="hide-hp-dmg",
        attack_id=attack.attack_id,
    )
    hp_change = apply_damage(
        HitPoints(current=30, maximum=30, temporary=4),
        damage.final_total,
        change_id="hide-hp-chg",
    )
    full = resolve_full_attack(
        attacker=attacker,
        defender=defender,
        attacker_kind="npc",
        attack_dice=[15],
        attack_id="hide-hp-full",
    )
    assert full.attack.outcome == "hit"
    event_type, payload, visibility = damage_domain_event(
        damage, hp_change, include_private=False
    )
    assert event_type == "damage.applied"
    blob = json.dumps(payload, default=str)
    for leaked in ('"current"', '"maximum"', '"temporary"', '"before"', '"after"'):
        assert leaked not in blob
    assert payload["hp_change"]["is_down"] is False  # observable outcome kept


def test_public_damage_event_hides_npc_mitigation_label():
    """Resistance/immunity labels stay DM-only on the public path (#226)."""
    import json

    attacker = pc_attacker(bonus=5)
    defender = defender_from_npc(
        armor_class=15, resistances=["fire"], immunities=["poison"]
    )
    spec = make_damage_spec(num_dice=1, die_size=8, modifier=2, damage_type="fire")
    damage = resolve_damage(
        spec=spec,
        damage_rolls=[6],
        attacker_kind="pc",
        defender=defender,
        damage_id="hide-mit-dmg",
    )
    assert damage.mitigation == "resistance"
    event_type, payload, _visibility = damage_domain_event(
        damage, None, include_private=False
    )
    assert event_type == "damage.applied"
    assert payload["final_total"] == 4  # observable damage stays
    blob = json.dumps(payload, default=str)
    assert "resistance" not in blob
    assert "mitigation" not in payload  # the classification label itself
    # DM-private payload keeps the label for audit/explanation.
    _, private_payload, _ = damage_domain_event(damage, None, include_private=True)
    assert private_payload["mitigation"] == "resistance"


def test_private_payloads_always_pair_with_private_visibility():
    """Private event payloads can never ride on public visibility (#226)."""
    attacker = pc_attacker(bonus=5)
    defender = npc_defender(ac=15)
    attack = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind="pc",
        dice=[12],
        ac_visibility="public",  # even fully public mechanics…
        attack_id="vis-atk",
    )
    _event_type, _payload, visibility = attack_domain_event(
        attack, include_private=True
    )
    assert visibility == "dm_private"

    spec = make_damage_spec(num_dice=1, die_size=8, modifier=1)
    damage = resolve_damage(
        spec=spec, damage_rolls=[4], attacker_kind="pc", damage_id="vis-dmg"
    )
    hp_change = apply_damage(
        HitPoints(current=10, maximum=10, temporary=0),
        damage.final_total,
        change_id="vis-hp",
    )
    _event_type, _payload, visibility = damage_domain_event(
        damage, hp_change, include_private=True, visibility="public"
    )
    assert visibility == "dm_private"  # …forced private despite caller arg

    # Same guarantee when damage resolved without an HP snapshot.
    _event_type, _payload, visibility = damage_domain_event(
        damage, None, include_private=True, visibility="public"
    )
    assert visibility == "dm_private"
    assert _payload["hp_change"] is None

    # Redacted projections keep their caller-chosen shared visibility.
    _event_type, _payload, visibility = damage_domain_event(
        damage, hp_change, include_private=False, visibility="public"
    )
    assert visibility == "public"


# ── Staged-effect promotion (PC sheet + NPC entity) ───────────────────────


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
    db.add(Profile(id=owner, email="handler@example.com"))
    camp = Campaign(id=_uuid.uuid4(), owner_id=owner, name="Handler", revision=0)
    db.add(camp)
    db.flush()
    char = Character(id=_uuid.uuid4(), owner_id=owner, system="dnd5e", name="Fighter")
    db.add(char)
    db.flush()
    # Canonical active roster (#266): Fighter is this campaign's selected PC.
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner", selected_character_id=char.id))
    sheet = Dnd5eCharacterSheet.from_frontend(
        {
            "name": "Fighter",
            "total_level": 3,
            "max_hp": 24,
            "current_hp": 20,
            "temp_hp": 6,
            "armor_class": 16,
            "weapons": [{"name": "Mace", "attack_bonus": 4, "damage": "1d6+2"}],
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
            "armor_class": 13,
            "hit_points": {"current": 30, "maximum": 30, "temporary": 4},
        },
    )
    db.add(brute)
    db.flush()
    turn = DmTurn(
        id=_uuid.uuid4(),
        campaign_id=camp.id,
        thread_id=f"thread:{_uuid.uuid4()}",
        audience="campaign",
        status="pending",
        source_revision=0,
        input_set_revision=1,
        submission_ids=[],
    )
    db.add(turn)
    db.flush()
    attempt = DmTurnAttempt(
        id=_uuid.uuid4(),
        turn_id=turn.id,
        attempt_number=1,
        status="prepared",
        campaign_id=camp.id,
        thread_id=turn.thread_id,
        audience="campaign",
        source_revision=0,
        input_set_revision=1,
        submission_ids=[],
    )
    db.add(attempt)
    db.commit()
    return camp, turn, attempt, char, brute


def test_staged_effect_applies_to_pc_sheet():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
        damage = resolve_damage(
            spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="h-pc-dmg"
        )
        effect = build_damage_effect(
            effect_id="h-pc-eff",
            target_kind="pc",
            target_id=str(char.id),
            damage=damage,
            visibility="public",
        )
        apply_staged_effects(db, camp, [effect], turn, attempt)
        db.commit()
        from models.characters import Dnd5eCharacterSheet
        from sqlalchemy import select

        sheet = (
            db.execute(
                select(Dnd5eCharacterSheet).where(
                    Dnd5eCharacterSheet.character_id == char.id
                )
            )
            .scalars()
            .first()
        )
        # 9 damage: 6 temp absorbed, 3 to current (20 -> 17), temp 0
        assert sheet.hit_points_temp == 0
        assert sheet.hit_points_current == 17


def test_staged_effect_applies_to_npc_entity():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, brute = _handler_fixture(db)
        spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
        damage = resolve_damage(
            spec=spec, damage_rolls=[6], attacker_kind="npc", damage_id="h-npc-dmg"
        )
        effect = build_damage_effect(
            effect_id="h-npc-eff",
            target_kind="npc",
            target_id=str(brute.id),
            damage=damage,
        )
        apply_staged_effects(db, camp, [effect], turn, attempt)
        db.commit()
        from models.world import WorldEntity

        row = db.get(WorldEntity, brute.id)
        # 9 damage: 4 temp absorbed, 5 to current (30 -> 25), temp 0
        assert row.details["hit_points"] == {
            "current": 25,
            "maximum": 30,
            "temporary": 0,
        }


def test_staged_effect_missing_target_fails_closed():
    from app.dm.effects import apply_staged_effects

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, _brute = _handler_fixture(db)
        spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
        damage = resolve_damage(
            spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="h-miss-dmg"
        )
        effect = build_damage_effect(
            effect_id="h-miss-eff",
            target_kind="pc",
            target_id=str(uuid.uuid4()),
            damage=damage,
        )
        with pytest.raises(ValueError, match="not found"):
            apply_staged_effects(db, camp, [effect], turn, attempt)


def test_staged_effect_rejects_character_outside_campaign_roster():
    """Cross-campaign guard: a PC effect only applies to rostered characters."""
    import uuid as _uuid

    from app.dm.effects import apply_staged_effects
    from models.characters import Character, Dnd5eCharacterSheet
    from models.profiles import Profile

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, _char, _brute = _handler_fixture(db)
        # Foreign character: owned elsewhere, never rostered in this campaign.
        outsider_owner = _uuid.uuid4()
        db.add(Profile(id=outsider_owner, email="outsider@example.com"))
        outsider = Character(
            id=_uuid.uuid4(), owner_id=outsider_owner, system="dnd5e", name="Outsider"
        )
        db.add(outsider)
        db.flush()
        sheet = Dnd5eCharacterSheet.from_frontend(
            {
                "name": "Outsider",
                "total_level": 3,
                "max_hp": 24,
                "current_hp": 24,
                "temp_hp": 0,
            },
            owner_id=outsider_owner,
        )
        sheet.character_id = outsider.id
        db.add(sheet)
        db.commit()
        hp_before = (sheet.hit_points_current, sheet.hit_points_temp)

        spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
        damage = resolve_damage(
            spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="h-xcamp-dmg"
        )
        effect = build_damage_effect(
            effect_id="h-xcamp-eff",
            target_kind="pc",
            target_id=str(outsider.id),
            damage=damage,
            visibility="public",
        )
        with pytest.raises(ValueError, match="not on this campaign's active roster"):
            apply_staged_effects(db, camp, [effect], turn, attempt)
        db.rollback()
        from sqlalchemy import select

        row = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == outsider.id
            )
        ).scalars().first()
        assert (row.hit_points_current, row.hit_points_temp) == hp_before  # untouched


def test_built_effect_survives_canonical_contract_validation():
    """The staged damage effect passes the typed DM contract (#206 path)."""
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    spec = make_damage_spec(num_dice=1, die_size=8, modifier=3, damage_type="slashing")
    damage = resolve_damage(
        spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="ctr-dmg"
    )
    effect = build_damage_effect(
        effect_id="ctr-eff-1",
        target_kind="pc",
        target_id=str(uuid.uuid4()),
        damage=damage,
        visibility="public",
    )
    claim = {
        "text": "The mace connects.",
        "claim_kind": "observation",
        "origin": "dm_adjudication",
        "visibility": "public",
    }
    c = normalize_contract(
        {
            "contract_version": CONTRACT_VERSION,
            "mode": "respond",
            "reason": "attack lands",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [claim]}],
            "staged_effects": [effect],
        }
    )
    assert len(c.staged_effects) == 1
    assert c.staged_effects[0].effect_type == "apply_attack_damage"
    # Malformed damage args still fail closed at the contract boundary.
    from app.dm.contract import ContractValidationError

    bad = dict(effect)
    bad["arguments"] = dict(effect["arguments"], damage_total=-5)
    with pytest.raises(ContractValidationError):
        normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "respond",
                "reason": "x",
                "beats": [
                    {
                        "id": "beat_1",
                        "type": "narration",
                        "claims": [
                            {
                                "text": "Boom.",
                                "claim_kind": "observation",
                                "origin": "dm_adjudication",
                                "visibility": "public",
                            }
                        ],
                    }
                ],
                "staged_effects": [bad],
            }
        )


def _respond_contract(*effects):
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


def test_provider_authored_damage_is_rejected_by_rules_validator():
    """A forged model damage_total never becomes authoritative (#226)."""
    from app.dm.validators import RulesValidator

    spec = make_damage_spec(num_dice=2, die_size=6, modifier=5, damage_type="slashing")
    damage = resolve_damage(
        spec=spec, damage_rolls=[6, 6], attacker_kind="pc", damage_id="forge-dmg"
    )
    forged = build_damage_effect(
        effect_id="forge-eff",
        target_kind="pc",
        target_id=str(uuid.uuid4()),
        damage=damage,
        visibility="public",
    )
    # Tamper the total the way model arithmetic would: still contract-valid…
    forged["arguments"] = dict(forged["arguments"], damage_total=9999)
    contract = _respond_contract(forged)
    result = RulesValidator().validate(contract, object())
    assert result.passed is False
    assert [v.code for v in result.violations] == ["provider_authored_damage"]

    # …while a damage-free contract passes the same validator.
    clean = _respond_contract()
    assert RulesValidator().validate(clean, object()).passed is True


def test_provider_schema_excludes_code_built_damage_effect():
    from app.dm.contract import contract_json_schema_strict

    schema = contract_json_schema_strict()
    effect_type_enum = schema["$defs"]["StagedEffect"]["properties"]["effect_type"][
        "enum"
    ]
    assert "apply_attack_damage" not in effect_type_enum
    assert "start_encounter" in effect_type_enum  # provider effects untouched


def test_duplicate_logical_damage_id_cannot_apply_twice_in_one_commit():
    """Two staged entries sharing one damage_id fail before any HP write."""
    from app.dm.effects import apply_staged_effects
    from models.characters import Dnd5eCharacterSheet
    from sqlalchemy import select

    factory = _handler_db()
    with factory() as db:
        camp, turn, attempt, char, _brute = _handler_fixture(db)
        spec = make_damage_spec(num_dice=1, die_size=8, modifier=3)
        damage = resolve_damage(
            spec=spec, damage_rolls=[6], attacker_kind="pc", damage_id="dup-dmg-1"
        )
        first = build_damage_effect(
            effect_id="dup-eff-a",
            target_kind="pc",
            target_id=str(char.id),
            damage=damage,
            visibility="public",
        )
        second = build_damage_effect(
            effect_id="dup-eff-b",
            target_kind="pc",
            target_id=str(char.id),
            damage=damage,
            visibility="public",
        )
        assert first["id"] != second["id"]  # distinct staged IDs, same damage
        with pytest.raises(ValueError, match="Duplicate logical damage_id"):
            apply_staged_effects(db, camp, [first, second], turn, attempt)
        db.rollback()
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == char.id
            )
        ).scalars().first()
        assert (sheet.hit_points_current, sheet.hit_points_temp) == (20, 6)

        # Distinct logical damage records still each apply exactly once.
        other = resolve_damage(
            spec=spec, damage_rolls=[4], attacker_kind="pc", damage_id="dup-dmg-2"
        )
        third = build_damage_effect(
            effect_id="dup-eff-c",
            target_kind="pc",
            target_id=str(char.id),
            damage=other,
            visibility="public",
        )
        apply_staged_effects(db, camp, [first, third], turn, attempt)
        db.commit()
        sheet = db.execute(
            select(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == char.id
            )
        ).scalars().first()
        # 9 + 7 = 16 damage: 6 temp absorbed, 10 to current (20 -> 10).
        assert sheet.hit_points_temp == 0
        assert sheet.hit_points_current == 10
