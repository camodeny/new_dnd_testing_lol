import json

from models import (
    db, CharacterClass, CharacterSkill, CharacterSavingThrow,
    CharacterProficiency, CharacterFeature, CharacterWeapon,
    CharacterEquipment, CharacterSpell, CharacterNote, CharacterResource,
    CharacterCompanion, CharacterCondition,
)

CHARACTER_RELATION_NAMES = (
    'classes',
    'skills',
    'saving_throws',
    'proficiencies',
    'features',
    'weapons',
    'equipment',
    'spells',
    'notes',
    'resources',
    'companions',
    'conditions',
)

CHARACTER_FIELD_GROUPS = {
    None: (
        'name',
        'player_name',
        'race',
        'subrace',
        'alignment',
        'background',
        'experience_points',
        'total_level',
    ),
    'ability_scores': ('strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma'),
    'combat': (
        'max_hp',
        'current_hp',
        'temp_hp',
        'armor_class',
        'initiative_bonus',
        'speed',
        'death_save_successes',
        'death_save_failures',
    ),
    'general': (
        'inspiration',
        'proficiency_bonus',
        'passive_perception',
        'exhaustion_level',
        'encumbrance_status',
    ),
    'spellcasting': ('spellcasting_ability', 'spell_save_dc', 'spell_attack_bonus'),
    'currency': ('cp', 'sp', 'ep', 'gp', 'pp'),
    'personality': ('personality_traits', 'ideals', 'bonds', 'flaws'),
    'appearance': ('age', 'height', 'weight', 'eyes', 'skin', 'hair', 'character_appearance'),
    'background_details': ('backstory', 'allies_organizations', 'additional_features_traits', 'treasure'),
}

CHARACTER_RELATION_CONFIGS = {
    'classes': {
        'model': CharacterClass,
        'fields': (
            ('class_name', 'class_name', ''),
            ('subclass', 'subclass', None),
            ('level', 'level', 1),
            ('hit_die_type', 'hit_die_type', 'd8'),
        ),
    },
    'skills': {
        'model': CharacterSkill,
        'fields': (
            ('skill_name', 'skill_name', ''),
            ('is_proficient', 'is_proficient', False),
            ('is_expertise', 'is_expertise', False),
            ('bonus_override', 'bonus_override', None),
        ),
    },
    'saving_throws': {
        'model': CharacterSavingThrow,
        'fields': (
            ('ability', 'ability', ''),
            ('is_proficient', 'is_proficient', False),
            ('bonus_override', 'bonus_override', None),
        ),
    },
    'proficiencies': {
        'model': CharacterProficiency,
        'fields': (
            ('proficiency_type', 'proficiency_type', ''),
            ('name', 'name', ''),
            ('notes', 'notes', None),
        ),
    },
    'features': {
        'model': CharacterFeature,
        'fields': (
            ('name', 'name', ''),
            ('source', 'source', None),
            ('description', 'description', None),
            ('uses_max', 'uses_max', None),
            ('uses_current', 'uses_current', None),
            ('recharge', 'recharge', None),
        ),
    },
    'weapons': {
        'model': CharacterWeapon,
        'fields': (
            ('name', 'name', ''),
            ('attack_bonus', 'attack_bonus', 0),
            ('damage', 'damage', None),
            ('damage_type', 'damage_type', None),
            ('properties', 'properties', None),
            ('notes', 'notes', None),
            ('is_equipped', 'is_equipped', False),
        ),
    },
    'equipment': {
        'model': CharacterEquipment,
        'fields': (
            ('name', 'name', ''),
            ('equipment_type', 'equipment_type', None),
            ('description', 'description', None),
            ('quantity', 'quantity', 1),
            ('weight', 'weight', None),
            ('is_equipped', 'is_equipped', False),
            ('armor_bonus', 'armor_bonus', None),
            ('properties', 'properties', None),
        ),
    },
    'spells': {
        'model': CharacterSpell,
        'fields': (
            ('name', 'name', ''),
            ('spell_level', 'spell_level', 0),
            ('school', 'school', None),
            ('casting_time', 'casting_time', None),
            ('range', 'range_str', None),
            ('components', 'components', None),
            ('duration', 'duration', None),
            ('description', 'description', None),
            ('at_higher_levels', 'at_higher_levels', None),
            ('is_prepared', 'is_prepared', False),
            ('is_ritual', 'is_ritual', False),
            ('is_concentration', 'is_concentration', False),
        ),
    },
    'notes': {
        'model': CharacterNote,
        'fields': (
            ('title', 'title', None),
            ('content', 'content', None),
        ),
    },
    'resources': {
        'model': CharacterResource,
        'fields': (
            ('name', 'name', ''),
            ('current', 'current', 0),
            ('max', 'max_amount', 0),
            ('recharge', 'recharge', None),
        ),
    },
    'companions': {
        'model': CharacterCompanion,
        'fields': (
            ('name', 'name', ''),
            ('companion_type', 'companion_type', None),
            ('max_hp', 'max_hp', 1),
            ('current_hp', 'current_hp', 1),
            ('armor_class', 'armor_class', None),
            ('speed', 'speed', None),
            ('description', 'description', None),
            ('notes', 'notes', None),
        ),
    },
    'conditions': {
        'model': CharacterCondition,
        'fields': (
            ('condition_name', 'condition_name', ''),
            ('description', 'description', None),
            ('source', 'source', None),
            ('is_permanent', 'is_permanent', False),
            ('duration_remaining', 'duration_remaining', None),
        ),
    },
}

CHARACTER_RELATION_ALIASES = {
    'classes': {
        'class_name': ('name', 'class', 'className'),
        'hit_die_type': ('hit_die', 'hitDie', 'hitDieType'),
    },
    'skills': {
        'skill_name': ('name', 'skill', 'skillName'),
    },
    'saving_throws': {
        'ability': ('name', 'saving_throw', 'savingThrow'),
    },
    'proficiencies': {
        'proficiency_type': ('type', 'proficiencyType'),
        'name': ('proficiency_name', 'proficiencyName'),
    },
    'features': {
        'name': ('feature_name', 'featureName', 'title'),
    },
    'weapons': {
        'name': ('weapon_name', 'weaponName', 'title'),
    },
    'equipment': {
        'equipment_type': ('type', 'item_type', 'itemType', 'equipmentType'),
        'name': ('item_name', 'itemName', 'equipment_name', 'equipmentName', 'title'),
    },
    'spells': {
        'name': ('spell_name', 'spellName', 'title'),
        'spell_level': ('level', 'spellLevel'),
    },
    'notes': {
        'title': ('name', 'note_title', 'noteTitle'),
        'content': ('description', 'text', 'notes'),
    },
    'resources': {
        'name': ('resource_name', 'resourceName', 'title'),
        'max': ('maximum', 'max_amount', 'maxAmount'),
    },
    'companions': {
        'name': ('companion_name', 'companionName', 'title'),
        'companion_type': ('type', 'companionType'),
    },
    'conditions': {
        'condition_name': ('name', 'condition', 'conditionName', 'title'),
    },
}

CHARACTER_RELATION_STRING_LIST_FIELDS = {
    'proficiencies': 'name',
}


def _first_present_value(item, field_name, aliases):
    for key in (field_name, *aliases.get(field_name, ())):
        if key in item:
            return item[key]
    return None


def _coerce_scalar(value):
    if isinstance(value, list):
        return ', '.join(str(item) for item in value if item is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _normalize_relation_item(relation_name, item, config):
    first_input_field = config['fields'][0][0]
    if isinstance(item, str):
        input_field = CHARACTER_RELATION_STRING_LIST_FIELDS.get(relation_name, first_input_field)
        item = {input_field: item}
    elif not isinstance(item, dict) or item is None:
        item = {}

    aliases = CHARACTER_RELATION_ALIASES.get(relation_name, {})
    values = {}
    for input_field, model_field, default in config['fields']:
        value = _first_present_value(item, input_field, aliases)
        values[model_field] = _coerce_scalar(default if value is None else value)
    return values


def character_full_dict(character):
    """Return a character dict with all nested relations."""
    data = character.to_dict()
    for relation_name in CHARACTER_RELATION_NAMES:
        data[relation_name] = [item.to_dict() for item in getattr(character, relation_name)]
    return data


def build_character_from_data(character, data):
    """Populate a Character model instance from JSON data."""
    for group_name, field_names in CHARACTER_FIELD_GROUPS.items():
        source = data if group_name is None else data.get(group_name, {})
        for field_name in field_names:
            setattr(character, field_name, source.get(field_name, getattr(character, field_name)))

    spellcasting = data.get('spellcasting', {})
    slots = spellcasting.get('spell_slots', {})
    for i in range(1, 10):
        key = str(i)
        if key in slots:
            setattr(character, f'spell_slots_level_{i}', slots[key].get('max', getattr(character, f'spell_slots_level_{i}')))
            setattr(character, f'spell_slots_used_{i}', slots[key].get('used', getattr(character, f'spell_slots_used_{i}')))


def update_character_relations(character, data):
    """Replace all nested relations from JSON data."""
    for relation_name, config in CHARACTER_RELATION_CONFIGS.items():
        if relation_name not in data:
            continue

        for existing in list(getattr(character, relation_name)):
            db.session.delete(existing)

        model = config['model']
        for item in data[relation_name]:
            values = _normalize_relation_item(relation_name, item, config)
            db.session.add(model(character=character, **values))
