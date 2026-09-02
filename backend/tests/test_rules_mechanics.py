"""Unit fixtures for #224 — authoritative character mechanical queries."""

import uuid
import pytest
from app.rules.mechanics import (
    MechanicsError,
    ability_modifier,
    get_character_mechanics_for_sheet,
    proficiency_for_level,
    query_armor_class,
    query_attacks,
    query_hit_points,
    query_initiative,
    query_passive_perception,
    query_skill_modifier,
    query_save_modifier,
    query_spellcasting,
    query_speed,
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
        # skill profs
        skills = ["acrobatics","animal_handling","arcana","athletics","deception","history","insight","intimidation","investigation","medicine","nature","perception","performance","persuasion","religion","sleight_of_hand","stealth","survival"]
        for s in skills:
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
        self.hit_dice = kwargs.pop("hit_dice", None)
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
        # consume extras
        for k, v in kwargs.items():
            setattr(self, k, v)
        import datetime
        self.updated_at = kwargs.get("updated_at") or datetime.datetime.now(datetime.timezone.utc)
        self.created_at = self.updated_at


def test_proficiency_by_level():
    assert proficiency_for_level(1) == 2
    assert proficiency_for_level(4) == 2
    assert proficiency_for_level(5) == 3
    assert proficiency_for_level(8) == 3
    assert proficiency_for_level(9) == 4
    assert proficiency_for_level(13) == 5
    assert proficiency_for_level(17) == 6
    assert proficiency_for_level(20) == 6


def test_ability_modifiers():
    assert ability_modifier(10) == 0
    assert ability_modifier(12) == 1
    assert ability_modifier(8) == -1
    assert ability_modifier(1) == -5
    assert ability_modifier(30) == 10
    assert ability_modifier(14) == 2
    assert ability_modifier(15) == 2
    assert ability_modifier(16) == 3


def test_level_class_scores_proficiency():
    sheet = FakeSheet(level=5, strength=16, dexterity=14, proficiency_bonus=3)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.proficiency.derived == 3
    assert m.proficiency.value == 3
    assert m.abilities["strength"].modifier == 3
    assert m.abilities["dexterity"].modifier == 2
    assert m.meta.model_version == "mechanics_v1"
    assert m.identity["level"] == 5


def test_proficiency_override_detected():
    sheet = FakeSheet(level=5, proficiency_bonus=2)  # derived is 3, stored 2
    m = get_character_mechanics_for_sheet(sheet)
    assert m.proficiency.derived == 3
    assert m.proficiency.value == 2
    assert m.proficiency.override_active is True
    assert m.proficiency.conflict is True
    assert any(w["code"] == "proficiency_mismatch" for w in m.validation.warnings)


def test_skills_saves_passive():
    # wis 14 (+2), prof 2, perception proficient, stealth not, expertise on perception via dict
    sheet = FakeSheet(
        level=1, wisdom=14, dexterity=12, proficiency_bonus=2,
        perception_prof=True, stealth_prof=False,
        skill_expertise={"perception": True},
    )
    m = get_character_mechanics_for_sheet(sheet)
    # perception: 2 (wis) +2 prof +2 expertise =6
    perc = m.skills["perception"]
    assert perc.proficient is True
    assert perc.expertise is True
    assert perc.derived == 6
    assert perc.modifier == 6
    # stealth: dex 12 (+1), not proficient
    stealth = m.skills["stealth"]
    assert stealth.proficient is False
    assert stealth.derived == 1
    # passive perception =10+6=16
    assert m.passive["perception"].derived == 16
    assert m.passive["perception"].value == 16
    # save: wis not proficient (default), str not
    wis_save = m.saves["wisdom"]
    assert wis_save.proficient is False
    assert wis_save.derived == 2  # wis mod

    # add save proficiency via boolean column
    sheet2 = FakeSheet(level=1, wisdom=14, proficiency_bonus=2, wis_save_prof=True)
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.saves["wisdom"].proficient is True
    assert m2.saves["wisdom"].derived == 4  # 2 +2


def test_skill_expertise_via_jsonb_list():
    sheet = FakeSheet(
        level=3, wisdom=12, intelligence=14, proficiency_bonus=2,
        skills=[
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": True},
            {"skill_name": "Arcana", "is_proficient": True},
        ],
    )
    m = get_character_mechanics_for_sheet(sheet)
    # perception: wis 12 (+1) +2 +2 expertise =5
    assert m.skills["perception"].derived == 5
    assert m.skills["perception"].expertise is True
    # arcana: int 14 (+2) +2 =4
    assert m.skills["arcana"].derived == 4
    # skill override via bonus_override
    sheet2 = FakeSheet(
        level=3, wisdom=12, proficiency_bonus=2,
        skills=[{"skill_name": "perception", "is_proficient": True, "bonus_override": 9}],
    )
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.skills["perception"].override == 9
    assert m2.skills["perception"].override_active is True
    assert m2.skills["perception"].effective == 9
    assert m2.skills["perception"].conflict is True  # derived 3 vs 9


def test_save_prof_via_jsonb_list_alias():
    sheet = FakeSheet(
        level=5, strength=16, proficiency_bonus=3,
        saving_throws=[{"ability": "strength", "is_proficient": True}],
    )
    m = get_character_mechanics_for_sheet(sheet)
    # str 16 (+3) +3 prof =6
    assert m.saves["strength"].derived == 6
    assert m.saves["strength"].proficient is True


def test_passive_perception_override():
    # wis 10 (0), prof2, perception proficient => derived 12
    sheet = FakeSheet(level=1, wisdom=10, proficiency_bonus=2, perception_prof=True, passive_perception=99)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.passive["perception"].derived == 12
    assert m.passive["perception"].override == 99
    assert m.passive["perception"].effective == 99
    assert m.passive["perception"].override_active is True
    assert m.passive["perception"].conflict is True


def test_ac_initiative_speed_hp():
    sheet = FakeSheet(
        level=1, dexterity=16, armor_class=15, initiative_bonus=1, speed=30,
        hit_points_max=12, hit_points_current=8, hit_points_temp=5,
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert m.combat["armor_class"]["value"] == 15
    assert m.combat["speed"]["value"] == 30
    assert m.combat["hit_points"]["maximum"] == 12
    assert m.combat["hit_points"]["current"] == 8
    assert m.combat["hit_points"]["temporary"] == 5
    # initiative: dex 16 (+3) +1 bonus =4
    assert m.combat["initiative"]["modifier"] == 4
    assert m.combat["initiative"]["dex_modifier"] == 3
    assert query_initiative(sheet).modifier == 4
    assert query_armor_class(sheet).value == 15
    assert query_speed(sheet).value == 30
    hp = query_hit_points(sheet)
    assert hp.maximum == 12 and hp.current == 8


def test_weapon_attack_data_alias():
    sheet = FakeSheet(
        weapons=[{"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "slashing", "is_equipped": True}]
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert len(m.attacks) == 1
    assert m.attacks[0].name == "Longsword"
    assert m.attacks[0].attack_bonus == 5
    assert m.attacks[0].equipped is True

    # attacks alias (legacy)
    sheet2 = FakeSheet(attacks=[{"name": "Dagger", "attack_bonus": 4, "damage": "1d4+2"}])
    # FakeSheet sets weapons and attacks separately; to test alias, mimic ORM where weapons=None and attacks set
    sheet2.weapons = None
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert len(m2.attacks) == 1
    assert m2.attacks[0].name == "Dagger"

    assert len(query_attacks(sheet)) == 1


def test_spell_dc_attack():
    # int 16 (+3), level 5 => prof 3 => dc 14, attack 6
    sheet = FakeSheet(level=5, intelligence=16, proficiency_bonus=3, spellcasting_ability="Intelligence")
    m = get_character_mechanics_for_sheet(sheet)
    assert m.spellcasting.save_dc_derived == 14
    assert m.spellcasting.attack_bonus_derived == 6
    assert m.spellcasting.save_dc == 14
    sc = query_spellcasting(sheet)
    assert sc.save_dc == 14

    # override
    sheet2 = FakeSheet(level=5, intelligence=16, proficiency_bonus=3, spellcasting_ability="int", spell_save_dc=15, spell_attack_bonus=7)
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.spellcasting.save_dc_override == 15
    assert m2.spellcasting.save_dc_effective == 15
    assert m2.spellcasting.save_dc_conflict is True
    assert m2.spellcasting.attack_bonus_conflict is True


def test_spell_slots_validation():
    sheet = FakeSheet(spell_slots={"1": {"max": 4, "used": 2}, "9": {"max": 1, "used": 1}})
    m = get_character_mechanics_for_sheet(sheet)
    assert m.spellcasting.slots["1"]["remaining"] == 2
    assert m.spellcasting.slots["9"]["remaining"] == 0

    # used > max → warning
    sheet2 = FakeSheet(spell_slots={"1": {"max": 2, "used": 5}})
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert any(w["code"] == "spell_slot_used_exceeds_max" for w in m2.validation.warnings)


def test_resources_conditions():
    sheet = FakeSheet(
        resources=[{"name": "Ki", "current": 2, "max": 3, "recharge": "short rest"}],
        conditions=[{"condition_name": "Poisoned", "description": "disadvantage"}],
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert m.resources[0].name == "Ki"
    assert m.resources[0].current == 2
    assert m.conditions[0].condition_name == "Poisoned"


def test_malformed_and_invalid_sheet_blocks_mechanics():
    # Fail-closed: invalid ability/level/ malformed JSONB must raise MechanicsError, not return DTO with errors
    with pytest.raises(MechanicsError) as exc:
        get_character_mechanics_for_sheet(FakeSheet(strength=31, level=1))
    assert exc.value.code == "invalid_ability_score"
    assert exc.value.field == "strength"

    with pytest.raises(MechanicsError) as exc:
        get_character_mechanics_for_sheet(FakeSheet(level=0))
    assert exc.value.code == "invalid_level"

    with pytest.raises(MechanicsError) as exc:
        get_character_mechanics_for_sheet(FakeSheet(skills="notalist"))  # type: ignore
    assert exc.value.code == "malformed_jsonb"
    assert "skills" in str(exc.value) or exc.value.code == "malformed_jsonb"

    with pytest.raises(MechanicsError) as exc:
        get_character_mechanics_for_sheet(FakeSheet(spell_slots="bad"))  # type: ignore
    assert exc.value.code == "malformed_spell_slots"

    with pytest.raises(MechanicsError):
        get_character_mechanics_for_sheet(FakeSheet(hit_points_max=0))

    with pytest.raises(MechanicsError):
        get_character_mechanics_for_sheet(FakeSheet(speed=-5))

    with pytest.raises(MechanicsError):
        get_character_mechanics_for_sheet(FakeSheet(weapons="bad"))  # type: ignore

    with pytest.raises(MechanicsError):
        get_character_mechanics_for_sheet(FakeSheet(resources="bad"))  # type: ignore

    # invalid skill name in list is non-blocking warning, not error
    sheet5 = FakeSheet(skills=[{"skill_name": "not_a_skill", "is_proficient": True}])
    m5 = get_character_mechanics_for_sheet(sheet5)
    assert any(w["code"] == "unknown_skill_name" for w in m5.validation.warnings)


def test_invalid_sheet_does_not_yield_authoritative_modifier():
    # Proves that an invalid sheet cannot supply a usable modifier — callers must handle the exception
    sheet = FakeSheet(strength=50, level=1)
    with pytest.raises(MechanicsError):
        query_skill_modifier(sheet, "athletics")
    with pytest.raises(MechanicsError):
        query_save_modifier(sheet, "strength")
    with pytest.raises(MechanicsError):
        query_passive_perception(sheet)
    # _sheet_value must surface mechanics_error instead of invented stats
    from app.dm.context import _sheet_value
    import datetime

    bad = FakeSheet(strength=50, level=1)
    # Ensure FakeSheet has required attrs for _sheet_value
    bad.id = uuid.uuid4()
    bad.character_id = uuid.uuid4()
    bad.owner_id = uuid.uuid4()
    bad.updated_at = datetime.datetime.now(datetime.timezone.utc)
    bad.character_name = "Bad"
    bad.conditions = None
    bad.death_save_successes = 0
    bad.death_save_failures = 0
    bad.exhaustion_level = 0
    bad.hit_points_max = 10
    bad.hit_points_current = 10
    bad.hit_points_temp = 0
    bad.initiative_bonus = 0
    bad.level = 1
    bad.strength = 50
    bad.passive_perception = None
    bad.proficiency_bonus = 2
    bad.resources = None
    bad.saving_throws = None
    bad.skills = None
    bad.speed = 30
    bad.spell_slots = None
    # ensure all abilities present
    for ab in ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]:
        if not hasattr(bad, ab):
            setattr(bad, ab, 10)
    val = _sheet_value(bad)
    assert "mechanics_error" in val
    assert val["mechanics_error"]["code"] == "invalid_ability_score"
    assert "mechanics" not in val


def test_creator_round_trip_still_functional():
    """Existing CRUD round-trip: frontend payload → from_frontend → mechanics."""
    import uuid as uuid_lib
    from models import Dnd5eCharacterSheet
    owner = uuid_lib.uuid4()
    payload = {
        "name": "Roundtrip Hero",
        "total_level": 3,
        "strength": 15,
        "dexterity": 14,
        "wisdom": 13,
        "armor_class": 14,
        "speed": 30,
        "max_hp": 20,
        "current_hp": 18,
        "temp_hp": 0,
        "proficiency_bonus": 2,
        "spellcasting_ability": "wisdom",
        "weapons": [{"name": "Mace", "attack_bonus": 4, "damage": "1d6+2"}],
        "resources": [{"name": "Rage", "current": 1, "max": 2}],
        "conditions": [{"condition_name": "Blessed"}],
    }
    sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=owner)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.identity["name"] == "Roundtrip Hero"
    assert m.identity["level"] == 3
    assert m.abilities["strength"].modifier == 2
    assert m.combat["armor_class"]["value"] == 14
    assert m.attacks[0].name == "Mace"
    # spell dc: wis 13 (+1) + prof 2 => 11
    assert m.spellcasting.save_dc_derived == 11


def test_focused_queries_without_alias_knowledge():
    sheet = FakeSheet(level=1, wisdom=14, perception_prof=True, proficiency_bonus=2)
    # Callers don't need to know column aliases
    skill = query_skill_modifier(sheet, "perception")
    assert skill.modifier == 4  # wis +2 prof
    save = query_save_modifier(sheet, "wisdom")
    assert save.modifier == 2
    pp = query_passive_perception(sheet)
    assert pp.value == 14  # 10+4


def test_unknown_skill_ability_raises_explicit_error():
    sheet = FakeSheet()
    with pytest.raises(MechanicsError) as exc:
        query_skill_modifier(sheet, "not_a_skill")
    assert exc.value.code == "invalid_skill"
    with pytest.raises(MechanicsError):
        query_save_modifier(sheet, "luck")


def test_source_version_metadata_present():
    sheet = FakeSheet(level=2)
    m = get_character_mechanics_for_sheet(sheet)
    assert "sources" in m.provenance
    assert m.provenance["sources"][0]["source_type"] == "dnd5e_character_sheet"
    assert m.meta.model_version == "mechanics_v1"
    assert m.meta.rules_revision == "2024.5e"
    assert m.meta.sheet_version != "unknown"


def test_proficiency_override_is_consistent_across_dependent_mechanics():
    # Level 5 derived prof 3, stored 5 (effective 5). All derived vs effective must be internally consistent.
    # Fixture: Wis 14 (+2), Int 12 (+1), proficient perception, Arcana proficient, Wis save proficient, spellcasting Int.
    sheet = FakeSheet(
        level=5,
        strength=14,  # +2
        wisdom=14,  # +2
        intelligence=12,  # +1
        dexterity=10,
        proficiency_bonus=5,  # override: derived 3, effective 5
        perception_prof=True,
        arcana_prof=True,
        wis_save_prof=True,
        spellcasting_ability="intelligence",
    )
    m = get_character_mechanics_for_sheet(sheet)
    # Proficiency metadata
    assert m.proficiency.derived == 3
    assert m.proficiency.effective == 5
    assert m.proficiency.conflict is True
    assert m.proficiency.source == "override"

    # Save: Wis save proficient => derived 2+3=5, effective 2+5=7
    wis_save = m.saves["wisdom"]
    assert wis_save.derived == 5
    assert wis_save.effective == 7
    assert wis_save.modifier == 7
    assert wis_save.conflict is True  # effective != derived due to proficiency
    # Non-proficient save unchanged
    str_save = m.saves["strength"]
    assert str_save.derived == 2
    assert str_save.effective == 2

    # Skills: perception proficient => derived 2+3=5, effective 2+5=7
    perc = m.skills["perception"]
    assert perc.derived == 5
    assert perc.effective == 7
    assert perc.modifier == 7
    # arcana: int +1 + prof => derived 1+3=4, effective 1+5=6
    arcana = m.skills["arcana"]
    assert arcana.derived == 4
    assert arcana.effective == 6
    # passive perception derived 10+5=15, effective 10+7=17
    assert m.passive["perception"].derived == 15
    assert m.passive["perception"].effective == 17

    # Spell: derived DC 8+3+1=12, effective DC 8+5+1=14 (no explicit override)
    assert m.spellcasting.save_dc_derived == 12
    assert m.spellcasting.save_dc_effective == 14
    assert m.spellcasting.save_dc == 14
    assert m.spellcasting.attack_bonus_derived == 4  # 3+1
    assert m.spellcasting.attack_bonus_effective == 6  # 5+1
    assert m.spellcasting.attack_bonus == 6

    # Focused queries must also reflect effective values, not derived
    assert query_save_modifier(sheet, "wisdom").modifier == 7
    assert query_skill_modifier(sheet, "perception").modifier == 7
    assert query_skill_modifier(sheet, "perception").effective == 7
    assert query_passive_perception(sheet).effective == 17
    assert query_passive_perception(sheet).value == 17
    assert query_spellcasting(sheet).save_dc == 14
    assert query_spellcasting(sheet).save_dc_effective == 14

    # With explicit skill bonus_override, that override wins over proficiency effective, but derived still tracks proficiency derived
    sheet2 = FakeSheet(level=5, wisdom=14, proficiency_bonus=5, perception_prof=True, skills=[{"skill_name": "perception", "is_proficient": True, "bonus_override": 99}])
    m2 = get_character_mechanics_for_sheet(sheet2)
    # derived still 5 (wis 2 + derived prof 3), effective is explicit 99
    assert m2.skills["perception"].derived == 5
    assert m2.skills["perception"].effective == 99
    assert m2.skills["perception"].override_active is True


# ── Evidence handler campaign scoping ────────────────────────────────────

def _setup_campaign_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore
    from database import Base  # noqa: E402

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_evidence_handler_rejects_cross_campaign_character_id():
    from models import Campaign, CampaignMember, Character, Dnd5eCharacterSheet, Profile  # noqa: E402
    from app.dm.context import ContextAudience  # noqa: E402
    from app.dm.contract import EvidenceRequest  # noqa: E402
    from app.dm.tools.character_sheet import handle_ask_character_sheet  # noqa: E402

    db = _setup_campaign_db()
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    db.add_all([Profile(id=owner_a, email="a@test.com"), Profile(id=owner_b, email="b@test.com")])
    camp_a = Campaign(id=uuid.uuid4(), owner_id=owner_a, name="A", revision=0)
    camp_b = Campaign(id=uuid.uuid4(), owner_id=owner_b, name="B", revision=0)
    db.add_all([camp_a, camp_b])
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=camp_a.id, user_id=owner_a, role="owner"),
        CampaignMember(campaign_id=camp_b.id, user_id=owner_b, role="owner"),
    ])
    # Character owned by B's owner, associated with B via submission
    char_b = Character(id=uuid.uuid4(), owner_id=owner_b, system="dnd5e", name="Bob")
    db.add(char_b)
    db.flush()
    sheet_b = Dnd5eCharacterSheet.from_frontend({"name": "Bob", "total_level": 2}, owner_b)
    sheet_b.character_id = char_b.id
    db.add(sheet_b)
    # Submission linking char_b to camp_b (authoritative association)
    from models import PlayerSubmission, PlayerSubmissionSegment  # noqa: E402

    sub = PlayerSubmission(
        id=uuid.uuid4(),
        campaign_id=camp_b.id,
        user_id=owner_b,
        character_id=char_b.id,
        thread_id=str(uuid.uuid4()),
        audience="campaign",
        sequence=1,
        raw_content="hi",
    )
    db.add(sub)
    db.flush()
    db.add(PlayerSubmissionSegment(submission_id=sub.id, position=0, segment_type="ic", text="hi"))
    db.commit()

    # Request from camp_a audience asking for char_b via character_id — must be rejected (missing)
    aud_a = ContextAudience(campaign_id=str(camp_a.id), thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[str(owner_a)])
    req = EvidenceRequest(id="req1", tool="ask_character_sheet", question="q?", scope="character_id", character_id=str(char_b.id))
    res = handle_ask_character_sheet(req, aud_a, db=db)
    assert res.status == "missing"
    assert res.payload is None
    # Must not stamp camp_a onto auth with leaked character
    assert not any(str(char_b.id) in str(s.source_id) for s in res.sources)


def test_evidence_handler_party_scope_is_campaign_bound():
    from models import Campaign, CampaignMember, Character, Dnd5eCharacterSheet, Profile, PlayerSubmission, PlayerSubmissionSegment  # noqa: E402
    from app.dm.context import ContextAudience  # noqa: E402
    from app.dm.contract import EvidenceRequest  # noqa: E402
    from app.dm.tools.character_sheet import handle_ask_character_sheet  # noqa: E402

    db = _setup_campaign_db()
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    db.add_all([Profile(id=owner_a, email="a2@test.com"), Profile(id=owner_b, email="b2@test.com")])
    camp_a = Campaign(id=uuid.uuid4(), owner_id=owner_a, name="A2", revision=0)
    camp_b = Campaign(id=uuid.uuid4(), owner_id=owner_b, name="B2", revision=0)
    db.add_all([camp_a, camp_b])
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=camp_a.id, user_id=owner_a, role="owner"),
        CampaignMember(campaign_id=camp_b.id, user_id=owner_b, role="owner"),
    ])
    # Two characters, one per campaign
    char_a = Character(id=uuid.uuid4(), owner_id=owner_a, system="dnd5e", name="AChar")
    char_b = Character(id=uuid.uuid4(), owner_id=owner_b, system="dnd5e", name="BChar")
    db.add_all([char_a, char_b])
    db.flush()
    for ch, own in [(char_a, owner_a), (char_b, owner_b)]:
        s = Dnd5eCharacterSheet.from_frontend({"name": ch.name, "total_level": 1}, own)
        s.character_id = ch.id
        db.add(s)
    # Submissions linking each correctly
    for camp, own, ch in [(camp_a, owner_a, char_a), (camp_b, owner_b, char_b)]:
        sub = PlayerSubmission(id=uuid.uuid4(), campaign_id=camp.id, user_id=own, character_id=ch.id, thread_id=str(uuid.uuid4()), audience="campaign", sequence=1, raw_content="hi")
        db.add(sub)
        db.flush()
        db.add(PlayerSubmissionSegment(submission_id=sub.id, position=0, segment_type="ic", text="hi"))
    db.commit()

    # Party scope from camp_a must not leak BChar
    aud_a = ContextAudience(campaign_id=str(camp_a.id), thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[str(owner_a), str(owner_b)])
    req_party = EvidenceRequest(id="req_party", tool="ask_character_sheet", question="party?", scope="party")
    res = handle_ask_character_sheet(req_party, aud_a, db=db)
    assert res.status == "ok"
    # Payload should contain only AChar, not BChar — even though audience includes owner_b, BChar is not in camp_a submissions
    payload_str = str(res.payload)
    assert "AChar" in payload_str
    assert "BChar" not in payload_str


def test_evidence_handler_invalid_sheet_is_tool_failure_not_ok():
    from models import Campaign, CampaignMember, Character, Dnd5eCharacterSheet, Profile, PlayerSubmission, PlayerSubmissionSegment  # noqa: E402
    from app.dm.context import ContextAudience  # noqa: E402
    from app.dm.contract import EvidenceRequest  # noqa: E402
    from app.dm.tools.character_sheet import handle_ask_character_sheet  # noqa: E402

    db = _setup_campaign_db()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="c@test.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="C", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    char = Character(id=uuid.uuid4(), owner_id=owner, system="dnd5e", name="Bad")
    db.add(char)
    db.flush()
    sheet = Dnd5eCharacterSheet.from_frontend({"name": "Bad", "total_level": 1}, owner)
    sheet.character_id = char.id
    sheet.strength = 99  # invalid blocks mechanics
    db.add(sheet)
    sub = PlayerSubmission(id=uuid.uuid4(), campaign_id=camp.id, user_id=owner, character_id=char.id, thread_id=str(uuid.uuid4()), audience="campaign", sequence=1, raw_content="hi")
    db.add(sub)
    db.flush()
    db.add(PlayerSubmissionSegment(submission_id=sub.id, position=0, segment_type="ic", text="hi"))
    db.commit()

    aud = ContextAudience(campaign_id=str(camp.id), thread_id=str(uuid.uuid4()), audience="campaign", user_ids=[str(owner)])
    req = EvidenceRequest(id="req_bad", tool="ask_character_sheet", question="q?", scope="character_id", character_id=str(char.id))
    res = handle_ask_character_sheet(req, aud, db=db)
    assert res.status == "tool_failure"
    assert res.payload is not None
    assert "error" in str(res.payload) or "error" in res.payload
    # Must not return ok with usable mechanics
    if isinstance(res.payload, dict) and "error" not in res.payload:
        # If single char, payload is error dict
        assert False, "invalid sheet should not return ok mechanics"

