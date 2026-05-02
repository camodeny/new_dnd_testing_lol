from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os
from auth import auth_bp, token_required
import random
import json
import string
from models import (
    db, User, Campaign, Character, CharacterClass, CharacterSkill,
    CharacterSavingThrow, CharacterProficiency, CharacterFeature,
    CharacterWeapon, CharacterEquipment, CharacterSpell, CharacterNote,
    CharacterResource, CharacterCompanion, CharacterCondition,
    CampaignSession, SessionMessage, CampaignMember, CampaignInvite
)
from openrouter import get_dm_response

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dnd.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['JWT_EXPIRATION_HOURS'] = int(os.environ.get('JWT_EXPIRATION_HOURS', 24))

db.init_app(app)

with app.app_context():
    db.create_all()

    def _ensure_columns():
        from sqlalchemy import text
        engine = db.engine
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(campaign)"))
            columns = {row[1] for row in result.fetchall()}
            if 'status' not in columns:
                conn.execute(text("ALTER TABLE campaign ADD COLUMN status VARCHAR DEFAULT 'active'"))
            if 'last_played_at' not in columns:
                conn.execute(text("ALTER TABLE campaign ADD COLUMN last_played_at DATETIME"))
            if 'settings' not in columns:
                conn.execute(text("ALTER TABLE campaign ADD COLUMN settings TEXT"))
            if 'invite_code' not in columns:
                conn.execute(text("ALTER TABLE campaign ADD COLUMN invite_code VARCHAR(20)"))
            conn.commit()

    try:
        _ensure_columns()
    except Exception:
        pass


app.register_blueprint(auth_bp)


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/campaigns', methods=['GET'])
@token_required
def get_campaigns(current_user):
    campaigns = current_user.campaigns
    return jsonify({'campaigns': [c.to_dict() for c in campaigns]}), 200

@app.route('/api/campaigns/<int:campaign_id>', methods=['GET'])
@token_required
def get_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    data = campaign.to_dict()
    data['characters'] = [
        _character_full_dict(c) for c in campaign.characters
    ]
    data['active_session'] = None
    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        data['active_session'] = active.to_dict()
    return jsonify({'campaign': data}), 200


@app.route('/api/campaigns/<int:campaign_id>', methods=['PUT'])
@token_required
def update_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    if 'name' in data:
        campaign.name = data['name']
    if 'description' in data:
        campaign.description = data['description']
    if 'difficulty' in data:
        campaign.difficulty = data['difficulty']
    if 'seed' in data:
        campaign.seed = data['seed']
    if 'status' in data:
        campaign.status = data['status']
    if 'settings' in data:
        campaign.settings = json.dumps(data['settings'])
    db.session.commit()
    return jsonify({'campaign': campaign.to_dict()}), 200


@app.route('/api/campaigns/<int:campaign_id>', methods=['DELETE'])
@token_required
def delete_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(campaign)
    db.session.commit()
    return jsonify({'message': 'Campaign deleted'}), 200


@app.route('/api/campaigns', methods=['POST'])
@token_required
def create_campaign(current_user):
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'Missing required field: name'}), 400

    campaign = Campaign(
        name=data['name'],
        description=data.get('description', ''),
        difficulty=data.get('difficulty', ''),
        seed=data.get('seed', ''),
        user_id=current_user.id
    )

    db.session.add(campaign)
    db.session.commit()

    return jsonify({'message': 'Campaign created successfully', 'campaign': campaign.to_dict()}), 201


# ---------------------------------------------------------------------------
# Character CRUD
# ---------------------------------------------------------------------------

def _character_full_dict(character):
    """Return a character dict with all nested relations."""
    d = character.to_dict()
    d['classes'] = [c.to_dict() for c in character.classes]
    d['skills'] = [s.to_dict() for s in character.skills]
    d['saving_throws'] = [s.to_dict() for s in character.saving_throws]
    d['proficiencies'] = [p.to_dict() for p in character.proficiencies]
    d['features'] = [f.to_dict() for f in character.features]
    d['weapons'] = [w.to_dict() for w in character.weapons]
    d['equipment'] = [e.to_dict() for e in character.equipment]
    d['spells'] = [s.to_dict() for s in character.spells]
    d['notes'] = [n.to_dict() for n in character.notes]
    d['resources'] = [r.to_dict() for r in character.resources]
    d['companions'] = [c.to_dict() for c in character.companions]
    d['conditions'] = [c.to_dict() for c in character.conditions]
    return d


def _build_character_from_data(character, data):
    """Populate a Character model instance from JSON data."""
    # Basic info
    character.name = data.get('name', character.name)
    character.player_name = data.get('player_name', character.player_name)
    character.race = data.get('race', character.race)
    character.subrace = data.get('subrace', character.subrace)
    character.alignment = data.get('alignment', character.alignment)
    character.background = data.get('background', character.background)
    character.experience_points = data.get('experience_points', character.experience_points)
    character.total_level = data.get('total_level', character.total_level)

    # Ability scores
    ability_scores = data.get('ability_scores', {})
    character.strength = ability_scores.get('strength', character.strength)
    character.dexterity = ability_scores.get('dexterity', character.dexterity)
    character.constitution = ability_scores.get('constitution', character.constitution)
    character.intelligence = ability_scores.get('intelligence', character.intelligence)
    character.wisdom = ability_scores.get('wisdom', character.wisdom)
    character.charisma = ability_scores.get('charisma', character.charisma)

    # Combat
    combat = data.get('combat', {})
    character.max_hp = combat.get('max_hp', character.max_hp)
    character.current_hp = combat.get('current_hp', character.current_hp)
    character.temp_hp = combat.get('temp_hp', character.temp_hp)
    character.armor_class = combat.get('armor_class', character.armor_class)
    character.initiative_bonus = combat.get('initiative_bonus', character.initiative_bonus)
    character.speed = combat.get('speed', character.speed)
    character.death_save_successes = combat.get('death_save_successes', character.death_save_successes)
    character.death_save_failures = combat.get('death_save_failures', character.death_save_failures)

    # General
    general = data.get('general', {})
    character.inspiration = general.get('inspiration', character.inspiration)
    character.proficiency_bonus = general.get('proficiency_bonus', character.proficiency_bonus)
    character.passive_perception = general.get('passive_perception', character.passive_perception)
    character.exhaustion_level = general.get('exhaustion_level', character.exhaustion_level)
    character.encumbrance_status = general.get('encumbrance_status', character.encumbrance_status)

    # Spellcasting
    spellcasting = data.get('spellcasting', {})
    character.spellcasting_ability = spellcasting.get('spellcasting_ability', character.spellcasting_ability)
    character.spell_save_dc = spellcasting.get('spell_save_dc', character.spell_save_dc)
    character.spell_attack_bonus = spellcasting.get('spell_attack_bonus', character.spell_attack_bonus)
    slots = spellcasting.get('spell_slots', {})
    for i in range(1, 10):
        key = str(i)
        if key in slots:
            setattr(character, f'spell_slots_level_{i}', slots[key].get('max', getattr(character, f'spell_slots_level_{i}')))
            setattr(character, f'spell_slots_used_{i}', slots[key].get('used', getattr(character, f'spell_slots_used_{i}')))

    # Currency
    currency = data.get('currency', {})
    character.cp = currency.get('cp', character.cp)
    character.sp = currency.get('sp', character.sp)
    character.ep = currency.get('ep', character.ep)
    character.gp = currency.get('gp', character.gp)
    character.pp = currency.get('pp', character.pp)

    # Personality
    personality = data.get('personality', {})
    character.personality_traits = personality.get('personality_traits', character.personality_traits)
    character.ideals = personality.get('ideals', character.ideals)
    character.bonds = personality.get('bonds', character.bonds)
    character.flaws = personality.get('flaws', character.flaws)

    # Appearance
    appearance = data.get('appearance', {})
    character.age = appearance.get('age', character.age)
    character.height = appearance.get('height', character.height)
    character.weight = appearance.get('weight', character.weight)
    character.eyes = appearance.get('eyes', character.eyes)
    character.skin = appearance.get('skin', character.skin)
    character.hair = appearance.get('hair', character.hair)
    character.character_appearance = appearance.get('character_appearance', character.character_appearance)

    # Background details
    bg = data.get('background_details', {})
    character.backstory = bg.get('backstory', character.backstory)
    character.allies_organizations = bg.get('allies_organizations', character.allies_organizations)
    character.additional_features_traits = bg.get('additional_features_traits', character.additional_features_traits)
    character.treasure = bg.get('treasure', character.treasure)


def _update_character_relations(character, data):
    """Replace all nested relations from JSON data."""
    # Classes
    if 'classes' in data:
        for old in character.classes:
            db.session.delete(old)
        for item in data['classes']:
            db.session.add(CharacterClass(
                character=character,
                class_name=item.get('class_name', ''),
                subclass=item.get('subclass'),
                level=item.get('level', 1),
                hit_die_type=item.get('hit_die_type', 'd8'),
            ))

    # Skills
    if 'skills' in data:
        for old in character.skills:
            db.session.delete(old)
        for item in data['skills']:
            db.session.add(CharacterSkill(
                character=character,
                skill_name=item.get('skill_name', ''),
                is_proficient=item.get('is_proficient', False),
                is_expertise=item.get('is_expertise', False),
                bonus_override=item.get('bonus_override'),
            ))

    # Saving throws
    if 'saving_throws' in data:
        for old in character.saving_throws:
            db.session.delete(old)
        for item in data['saving_throws']:
            db.session.add(CharacterSavingThrow(
                character=character,
                ability=item.get('ability', ''),
                is_proficient=item.get('is_proficient', False),
                bonus_override=item.get('bonus_override'),
            ))

    # Proficiencies
    if 'proficiencies' in data:
        for old in character.proficiencies:
            db.session.delete(old)
        for item in data['proficiencies']:
            db.session.add(CharacterProficiency(
                character=character,
                proficiency_type=item.get('proficiency_type', ''),
                name=item.get('name', ''),
                notes=item.get('notes'),
            ))

    # Features
    if 'features' in data:
        for old in character.features:
            db.session.delete(old)
        for item in data['features']:
            db.session.add(CharacterFeature(
                character=character,
                name=item.get('name', ''),
                source=item.get('source'),
                description=item.get('description'),
                uses_max=item.get('uses_max'),
                uses_current=item.get('uses_current'),
                recharge=item.get('recharge'),
            ))

    # Weapons
    if 'weapons' in data:
        for old in character.weapons:
            db.session.delete(old)
        for item in data['weapons']:
            db.session.add(CharacterWeapon(
                character=character,
                name=item.get('name', ''),
                attack_bonus=item.get('attack_bonus', 0),
                damage=item.get('damage'),
                damage_type=item.get('damage_type'),
                properties=item.get('properties'),
                notes=item.get('notes'),
                is_equipped=item.get('is_equipped', False),
            ))

    # Equipment
    if 'equipment' in data:
        for old in character.equipment:
            db.session.delete(old)
        for item in data['equipment']:
            db.session.add(CharacterEquipment(
                character=character,
                name=item.get('name', ''),
                equipment_type=item.get('equipment_type'),
                description=item.get('description'),
                quantity=item.get('quantity', 1),
                weight=item.get('weight'),
                is_equipped=item.get('is_equipped', False),
                armor_bonus=item.get('armor_bonus'),
                properties=item.get('properties'),
            ))

    # Spells
    if 'spells' in data:
        for old in character.spells:
            db.session.delete(old)
        for item in data['spells']:
            db.session.add(CharacterSpell(
                character=character,
                name=item.get('name', ''),
                spell_level=item.get('spell_level', 0),
                school=item.get('school'),
                casting_time=item.get('casting_time'),
                range_str=item.get('range'),
                components=item.get('components'),
                duration=item.get('duration'),
                description=item.get('description'),
                at_higher_levels=item.get('at_higher_levels'),
                is_prepared=item.get('is_prepared', False),
                is_ritual=item.get('is_ritual', False),
                is_concentration=item.get('is_concentration', False),
            ))

    # Notes
    if 'notes' in data:
        for old in character.notes:
            db.session.delete(old)
        for item in data['notes']:
            db.session.add(CharacterNote(
                character=character,
                title=item.get('title'),
                content=item.get('content'),
            ))

    # Resources
    if 'resources' in data:
        for old in character.resources:
            db.session.delete(old)
        for item in data['resources']:
            db.session.add(CharacterResource(
                character=character,
                name=item.get('name', ''),
                current=item.get('current', 0),
                max_amount=item.get('max', 0),
                recharge=item.get('recharge'),
            ))

    # Companions
    if 'companions' in data:
        for old in character.companions:
            db.session.delete(old)
        for item in data['companions']:
            db.session.add(CharacterCompanion(
                character=character,
                name=item.get('name', ''),
                companion_type=item.get('companion_type'),
                max_hp=item.get('max_hp', 1),
                current_hp=item.get('current_hp', 1),
                armor_class=item.get('armor_class'),
                speed=item.get('speed'),
                description=item.get('description'),
                notes=item.get('notes'),
            ))

    # Conditions
    if 'conditions' in data:
        for old in character.conditions:
            db.session.delete(old)
        for item in data['conditions']:
            db.session.add(CharacterCondition(
                character=character,
                condition_name=item.get('condition_name', ''),
                description=item.get('description'),
                source=item.get('source'),
                is_permanent=item.get('is_permanent', False),
                duration_remaining=item.get('duration_remaining'),
            ))


@app.route('/api/characters', methods=['GET'])
@token_required
def get_characters(current_user):
    characters = Character.query.filter_by(user_id=current_user.id).all()
    return jsonify({'characters': [c.to_dict() for c in characters]}), 200


@app.route('/api/characters/<int:character_id>', methods=['GET'])
@token_required
def get_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify({'character': _character_full_dict(character)}), 200


@app.route('/api/characters', methods=['POST'])
@token_required
def create_character(current_user):
    data = request.get_json()
    if not data or not data.get('name') or not data.get('race'):
        return jsonify({'error': 'Missing required fields: name and race'}), 400

    character = Character(user_id=current_user.id)
    _build_character_from_data(character, data)
    character.campaign_id = data.get('campaign_id')

    db.session.add(character)
    db.session.flush()  # get character.id before creating relations
    _update_character_relations(character, data)
    db.session.commit()

    return jsonify({'message': 'Character created successfully', 'character': _character_full_dict(character)}), 201


@app.route('/api/characters/<int:character_id>', methods=['PUT'])
@token_required
def update_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json()
    _build_character_from_data(character, data)
    if 'campaign_id' in data:
        character.campaign_id = data['campaign_id']

    _update_character_relations(character, data)
    db.session.commit()

    return jsonify({'message': 'Character updated successfully', 'character': _character_full_dict(character)}), 200


@app.route('/api/characters/<int:character_id>', methods=['DELETE'])
@token_required
def delete_character(current_user, character_id):
    character = Character.query.get_or_404(character_id)
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403

    db.session.delete(character)
    db.session.commit()
    return jsonify({'message': 'Character deleted successfully'}), 200


# ---------------------------------------------------------------------------
# Dev helpers
# ---------------------------------------------------------------------------

_DEV_CHARACTER_TEMPLATES = [
    {
        "name": "Dev Test",
        "player_name": "Developer",
        "race": "Human",
        "subrace": "",
        "alignment": "Neutral Good",
        "background": "Soldier",
        "experience_points": 900,
        "total_level": 3,
        "ability_scores": {"strength": 16, "dexterity": 12, "constitution": 14, "intelligence": 10, "wisdom": 13, "charisma": 8},
        "combat": {"max_hp": 28, "current_hp": 28, "temp_hp": 0, "armor_class": 16, "initiative_bonus": 1, "speed": 30, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 11, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "", "spell_save_dc": None, "spell_attack_bonus": None, "spell_slots": {str(i): {"max": 0, "used": 0} for i in range(1, 10)}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 15, "pp": 0},
        "personality": {"personality_traits": "Likes to test things.", "ideals": "Code should work.", "bonds": "Bonded to the dev server.", "flaws": "Overconfident in unit tests."},
        "appearance": {"age": "25", "height": "5'10\"", "weight": "180", "eyes": "Blue", "skin": "Fair", "hair": "Brown", "character_appearance": "Looks like a developer."},
        "background_details": {"backstory": "Created by the /dev/character route.", "allies_organizations": "The Testing Guild", "additional_features_traits": "", "treasure": ""},
        "classes": [{"class_name": "Fighter", "subclass": "Champion", "level": 3, "hit_die_type": "d10"}],
        "skills": [
            {"skill_name": "Athletics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Strength", "is_proficient": True, "bonus_override": None},
            {"ability": "Constitution", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "All armor", "notes": None},
            {"proficiency_type": "Weapons", "name": "Martial weapons", "notes": None},
        ],
        "features": [
            {"name": "Second Wind", "source": "Fighter 1", "description": "Regain HP as a bonus action.", "uses_max": 1, "uses_current": 1, "recharge": "Short Rest"},
            {"name": "Action Surge", "source": "Fighter 2", "description": "Take one extra action on your turn.", "uses_max": 1, "uses_current": 1, "recharge": "Short Rest"},
        ],
        "weapons": [
            {"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "Slashing", "properties": "Versatile (1d10)", "notes": None, "is_equipped": True},
            {"name": "Longbow", "attack_bonus": 3, "damage": "1d8+1", "damage_type": "Piercing", "properties": "Ammunition, range 150/600", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Chain mail", "equipment_type": "Armor", "description": "Heavy armor.", "quantity": 1, "weight": 55, "is_equipped": True, "armor_bonus": 16, "properties": "Disadvantage on Stealth"},
            {"name": "Explorer's Pack", "equipment_type": "Adventuring Gear", "description": "Basic adventuring supplies.", "quantity": 1, "weight": None, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [],
        "notes": [{"title": "Dev Note", "content": "This character was auto-generated by the /dev/character route."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Elara Moonwhisper",
        "player_name": "Developer",
        "race": "Elf",
        "subrace": "High Elf",
        "alignment": "Chaotic Good",
        "background": "Sage",
        "experience_points": 2700,
        "total_level": 5,
        "ability_scores": {"strength": 8, "dexterity": 14, "constitution": 12, "intelligence": 16, "wisdom": 13, "charisma": 10},
        "combat": {"max_hp": 32, "current_hp": 32, "temp_hp": 0, "armor_class": 12, "initiative_bonus": 2, "speed": 30, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 3, "passive_perception": 12, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Intelligence", "spell_save_dc": 14, "spell_attack_bonus": 6, "spell_slots": {"1": {"max": 4, "used": 0}, "2": {"max": 3, "used": 0}, "3": {"max": 2, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 10, "ep": 0, "gp": 45, "pp": 0},
        "personality": {"personality_traits": "Curious about ancient lore.", "ideals": "Knowledge is the path to power.", "bonds": "My mentor's library must be protected.", "flaws": "I often overlook the obvious."},
        "appearance": {"age": "127", "height": "5'6\"", "weight": "130", "eyes": "Violet", "skin": "Pale", "hair": "Silver", "character_appearance": "Wears flowing blue robes adorned with arcane symbols."},
        "background_details": {"backstory": "Studied at the Tower of Arcana.", "allies_organizations": "The Order of the Silver Quill", "additional_features_traits": "Researcher", "treasure": ""},
        "classes": [{"class_name": "Wizard", "subclass": "School of Evocation", "level": 5, "hit_die_type": "d6"}],
        "skills": [
            {"skill_name": "Arcana", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "History", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Investigation", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Intelligence", "is_proficient": True, "bonus_override": None},
            {"ability": "Wisdom", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Weapons", "name": "Daggers", "notes": None},
            {"proficiency_type": "Weapons", "name": "Quarterstaffs", "notes": None},
            {"proficiency_type": "Tools", "name": "Calligrapher's Supplies", "notes": None},
        ],
        "features": [
            {"name": "Arcane Recovery", "source": "Wizard 1", "description": "Once per day recover spell slots.", "uses_max": 1, "uses_current": 1, "recharge": "Long Rest"},
            {"name": "Sculpt Spells", "source": "Wizard 2", "description": "Exclude creatures from evocation spells.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Quarterstaff", "attack_bonus": 4, "damage": "1d6-1", "damage_type": "Bludgeoning", "properties": "Versatile (1d8)", "notes": None, "is_equipped": True},
            {"name": "Dagger", "attack_bonus": 4, "damage": "1d4+2", "damage_type": "Piercing", "properties": "Finesse, light, thrown (20/60)", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Spellbook", "equipment_type": "Adventuring Gear", "description": "Contains all known spells.", "quantity": 1, "weight": 3, "is_equipped": False, "armor_bonus": None, "properties": None},
            {"name": "Component Pouch", "equipment_type": "Adventuring Gear", "description": "Material components for spells.", "quantity": 1, "weight": 2, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Fireball", "spell_level": 3, "school": "Evocation", "casting_time": "1 action", "range": "150 ft", "components": "V, S, M", "duration": "Instantaneous", "description": "A bright streak flashes and explodes.", "at_higher_levels": "+1d6 per slot above 3rd", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Magic Missile", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "120 ft", "components": "V, S", "duration": "Instantaneous", "description": "Three darts of magical force.", "at_higher_levels": "+1 dart per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Shield", "spell_level": 1, "school": "Abjuration", "casting_time": "1 reaction", "range": "Self", "components": "V, S", "duration": "1 round", "description": "+5 AC until start of next turn.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": False},
        ],
        "notes": [{"title": "Dev Note", "content": "Wizard template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Thorin Ironbeard",
        "player_name": "Developer",
        "race": "Dwarf",
        "subrace": "Hill Dwarf",
        "alignment": "Lawful Good",
        "background": "Acolyte",
        "experience_points": 650,
        "total_level": 2,
        "ability_scores": {"strength": 14, "dexterity": 10, "constitution": 16, "intelligence": 10, "wisdom": 16, "charisma": 12},
        "combat": {"max_hp": 20, "current_hp": 20, "temp_hp": 0, "armor_class": 18, "initiative_bonus": 0, "speed": 25, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 13, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Wisdom", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 3, "used": 0}, "2": {"max": 0, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 5, "ep": 0, "gp": 20, "pp": 0},
        "personality": {"personality_traits": "I quote sacred texts in every conversation.", "ideals": "Tradition must be preserved.", "bonds": "My temple is my family.", "flaws": "I am suspicious of other faiths."},
        "appearance": {"age": "85", "height": "4'8\"", "weight": "160", "eyes": "Brown", "skin": "Ruddy", "hair": "Black", "character_appearance": "Wears heavy plate armor with a holy symbol."},
        "background_details": {"backstory": "Raised in the Temple of Moradin.", "allies_organizations": "The Clergy of Moradin", "additional_features_traits": "Shelter of the Faithful", "treasure": ""},
        "classes": [{"class_name": "Cleric", "subclass": "Life Domain", "level": 2, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Medicine", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Insight", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Religion", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Wisdom", "is_proficient": True, "bonus_override": None},
            {"ability": "Charisma", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "All armor", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons", "notes": None},
        ],
        "features": [
            {"name": "Channel Divinity: Preserve Life", "source": "Cleric 2", "description": "Restore 5x cleric level HP to creatures.", "uses_max": 1, "uses_current": 1, "recharge": "Short Rest"},
            {"name": "Disciple of Life", "source": "Cleric 1", "description": "Healing spells restore extra 2+spell level HP.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Warhammer", "attack_bonus": 4, "damage": "1d8+2", "damage_type": "Bludgeoning", "properties": "Versatile (1d10)", "notes": None, "is_equipped": True},
            {"name": "Light Crossbow", "attack_bonus": 2, "damage": "1d8", "damage_type": "Piercing", "properties": "Ammunition, range 80/320, loading, two-handed", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Scale mail", "equipment_type": "Armor", "description": "Medium armor.", "quantity": 1, "weight": 45, "is_equipped": True, "armor_bonus": 14, "properties": None},
            {"name": "Holy Symbol", "equipment_type": "Adventuring Gear", "description": "Amulet of Moradin.", "quantity": 1, "weight": 1, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Cure Wounds", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "Touch", "components": "V, S", "duration": "Instantaneous", "description": "A creature regains 1d8+spellcasting modifier HP.", "at_higher_levels": "+1d8 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Bless", "spell_level": 1, "school": "Enchantment", "casting_time": "1 action", "range": "30 ft", "components": "V, S, M", "duration": "1 minute", "description": "Up to three creatures add d4 to attack rolls and saves.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": True},
        ],
        "notes": [{"title": "Dev Note", "content": "Cleric template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Pippin Underhill",
        "player_name": "Developer",
        "race": "Halfling",
        "subrace": "Lightfoot",
        "alignment": "Neutral Good",
        "background": "Criminal",
        "experience_points": 650,
        "total_level": 2,
        "ability_scores": {"strength": 8, "dexterity": 16, "constitution": 12, "intelligence": 13, "wisdom": 10, "charisma": 14},
        "combat": {"max_hp": 14, "current_hp": 14, "temp_hp": 0, "armor_class": 15, "initiative_bonus": 3, "speed": 25, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 10, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "", "spell_save_dc": None, "spell_attack_bonus": None, "spell_slots": {str(i): {"max": 0, "used": 0} for i in range(1, 10)}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 25, "pp": 0},
        "personality": {"personality_traits": "I always have a plan for what to do when things go wrong.", "ideals": "I steal from the wealthy to help those in need.", "bonds": "I'm trying to pay off an old debt.", "flaws": "When I see something valuable, I can't think about anything else."},
        "appearance": {"age": "32", "height": "3'2\"", "weight": "38", "eyes": "Green", "skin": "Tan", "hair": "Chestnut", "character_appearance": "Wears dark leather and moves without a sound."},
        "background_details": {"backstory": "Grew up on the streets of Waterdeep.", "allies_organizations": "The Shadow Thieves", "additional_features_traits": "Criminal Contact", "treasure": ""},
        "classes": [{"class_name": "Rogue", "subclass": "Thief", "level": 2, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Stealth", "is_proficient": True, "is_expertise": True, "bonus_override": None},
            {"skill_name": "Sleight of Hand", "is_proficient": True, "is_expertise": True, "bonus_override": None},
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Acrobatics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Dexterity", "is_proficient": True, "bonus_override": None},
            {"ability": "Intelligence", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons", "notes": None},
            {"proficiency_type": "Tools", "name": "Thieves' Tools", "notes": None},
        ],
        "features": [
            {"name": "Sneak Attack", "source": "Rogue 1", "description": "Extra 1d6 damage when advantage or ally nearby.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Cunning Action", "source": "Rogue 2", "description": "Bonus action to Dash, Disengage, or Hide.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Rapier", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "Piercing", "properties": "Finesse", "notes": None, "is_equipped": True},
            {"name": "Shortbow", "attack_bonus": 5, "damage": "1d6+3", "damage_type": "Piercing", "properties": "Ammunition, range 80/320, two-handed", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Leather armor", "equipment_type": "Armor", "description": "Light armor.", "quantity": 1, "weight": 10, "is_equipped": True, "armor_bonus": 11, "properties": None},
            {"name": "Thieves' Tools", "equipment_type": "Tools", "description": "Lockpicks and disabling gear.", "quantity": 1, "weight": 1, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [],
        "notes": [{"title": "Dev Note", "content": "Rogue template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Grashnak Bonecrusher",
        "player_name": "Developer",
        "race": "Half-Orc",
        "subrace": "",
        "alignment": "Chaotic Neutral",
        "background": "Outlander",
        "experience_points": 2700,
        "total_level": 5,
        "ability_scores": {"strength": 18, "dexterity": 14, "constitution": 16, "intelligence": 8, "wisdom": 12, "charisma": 10},
        "combat": {"max_hp": 55, "current_hp": 55, "temp_hp": 0, "armor_class": 15, "initiative_bonus": 2, "speed": 40, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 3, "passive_perception": 11, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "", "spell_save_dc": None, "spell_attack_bonus": None, "spell_slots": {str(i): {"max": 0, "used": 0} for i in range(1, 10)}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 8, "pp": 0},
        "personality": {"personality_traits": "I have a crude sense of humor.", "ideals": "Might makes right.", "bonds": "My tribe is my life.", "flaws": "I have little respect for weakness."},
        "appearance": {"age": "22", "height": "6'4\"", "weight": "250", "eyes": "Black", "skin": "Green", "hair": "Bald", "character_appearance": "Covered in scars and tribal tattoos."},
        "background_details": {"backstory": "Exiled from his tribe for showing mercy.", "allies_organizations": "The Iron Wolf Clan", "additional_features_traits": "Wanderer", "treasure": ""},
        "classes": [{"class_name": "Barbarian", "subclass": "Path of the Berserker", "level": 5, "hit_die_type": "d12"}],
        "skills": [
            {"skill_name": "Athletics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Survival", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Strength", "is_proficient": True, "bonus_override": None},
            {"ability": "Constitution", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor, medium armor, shields", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons, martial weapons", "notes": None},
        ],
        "features": [
            {"name": "Rage", "source": "Barbarian 1", "description": "Advantage on Str checks and saves, bonus damage, resistance to bludgeoning/piercing/slashing.", "uses_max": 3, "uses_current": 3, "recharge": "Long Rest"},
            {"name": "Reckless Attack", "source": "Barbarian 2", "description": "Advantage on melee Str attacks this turn, but attacks against you have advantage.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Extra Attack", "source": "Barbarian 5", "description": "Attack twice instead of once.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Greataxe", "attack_bonus": 7, "damage": "1d12+4", "damage_type": "Slashing", "properties": "Heavy, two-handed", "notes": None, "is_equipped": True},
            {"name": "Handaxes", "attack_bonus": 7, "damage": "1d6+4", "damage_type": "Slashing", "properties": "Light, thrown (20/60)", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Hide armor", "equipment_type": "Armor", "description": "Medium armor.", "quantity": 1, "weight": 12, "is_equipped": True, "armor_bonus": 12, "properties": None},
            {"name": "Explorer's Pack", "equipment_type": "Adventuring Gear", "description": "Basic adventuring supplies.", "quantity": 1, "weight": None, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [],
        "notes": [{"title": "Dev Note", "content": "Barbarian template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Seraphina Duskweaver",
        "player_name": "Developer",
        "race": "Tiefling",
        "subrace": "",
        "alignment": "Chaotic Neutral",
        "background": "Charlatan",
        "experience_points": 900,
        "total_level": 3,
        "ability_scores": {"strength": 8, "dexterity": 14, "constitution": 12, "intelligence": 10, "wisdom": 13, "charisma": 16},
        "combat": {"max_hp": 24, "current_hp": 24, "temp_hp": 0, "armor_class": 13, "initiative_bonus": 2, "speed": 30, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 11, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Charisma", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 2, "used": 0}, "2": {"max": 0, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 30, "pp": 0},
        "personality": {"personality_traits": "I lie about almost everything.", "ideals": "I am a free spirit—no one tells me what to do.", "bonds": "I fleeced the wrong person and must keep a low profile.", "flaws": "I can't resist swindling people who are more powerful than me."},
        "appearance": {"age": "24", "height": "5'7\"", "weight": "140", "eyes": "Gold", "skin": "Crimson", "hair": "Black", "character_appearance": "Small horns curl from her forehead; tail flicks when nervous."},
        "background_details": {"backstory": "Born in the city of Baldur's Gate.", "allies_organizations": "The Masked Lords", "additional_features_traits": "False Identity", "treasure": ""},
        "classes": [{"class_name": "Warlock", "subclass": "The Fiend", "level": 3, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Deception", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Arcana", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Persuasion", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Wisdom", "is_proficient": True, "bonus_override": None},
            {"ability": "Charisma", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons", "notes": None},
        ],
        "features": [
            {"name": "Dark One's Blessing", "source": "Warlock 1", "description": "When you reduce a hostile to 0 HP, gain temp HP.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Fiendish Vigor", "source": "Warlock 2", "description": "Cast False Life on yourself at will.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Light Crossbow", "attack_bonus": 4, "damage": "1d8+2", "damage_type": "Piercing", "properties": "Ammunition, range 80/320, loading, two-handed", "notes": None, "is_equipped": True},
            {"name": "Dagger", "attack_bonus": 4, "damage": "1d4+2", "damage_type": "Piercing", "properties": "Finesse, light, thrown (20/60)", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Leather armor", "equipment_type": "Armor", "description": "Light armor.", "quantity": 1, "weight": 10, "is_equipped": True, "armor_bonus": 11, "properties": None},
            {"name": "Component Pouch", "equipment_type": "Adventuring Gear", "description": "Material components.", "quantity": 1, "weight": 2, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Eldritch Blast", "spell_level": 0, "school": "Evocation", "casting_time": "1 action", "range": "120 ft", "components": "V, S", "duration": "Instantaneous", "description": "A beam of crackling energy.", "at_higher_levels": "+1 beam at 5th, 11th, 17th level", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Hex", "spell_level": 1, "school": "Enchantment", "casting_time": "1 bonus action", "range": "90 ft", "components": "V, S, M", "duration": "1 hour", "description": "Target takes extra 1d6 necrotic damage.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": True},
            {"name": "Burning Hands", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "Self (15 ft cone)", "components": "V, S", "duration": "Instantaneous", "description": "Each creature in cone takes 3d6 fire damage.", "at_higher_levels": "+1d6 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
        ],
        "notes": [{"title": "Dev Note", "content": "Warlock template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Aldric Stormblade",
        "player_name": "Developer",
        "race": "Dragonborn",
        "subrace": "Blue",
        "alignment": "Lawful Good",
        "background": "Noble",
        "experience_points": 2700,
        "total_level": 5,
        "ability_scores": {"strength": 16, "dexterity": 10, "constitution": 14, "intelligence": 10, "wisdom": 12, "charisma": 14},
        "combat": {"max_hp": 44, "current_hp": 44, "temp_hp": 0, "armor_class": 18, "initiative_bonus": 0, "speed": 30, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 3, "passive_perception": 11, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Charisma", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 4, "used": 0}, "2": {"max": 2, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 10, "ep": 0, "gp": 50, "pp": 2},
        "personality": {"personality_traits": "My eloquent flattery makes everyone feel special.", "ideals": "It is my duty to respect the authority of my superiors.", "bonds": "I will face any challenge to win the approval of my family.", "flaws": "I have an insatiable desire for decadent luxury."},
        "appearance": {"age": "28", "height": "6'2\"", "weight": "220", "eyes": "Azure", "skin": "Scales", "hair": "None", "character_appearance": "Proud bearing with bronze scales and a commanding presence."},
        "background_details": {"backstory": "Born into a noble house with a legacy of dragon riders.", "allies_organizations": "The Order of the Gauntlet", "additional_features_traits": "Position of Privilege", "treasure": ""},
        "classes": [{"class_name": "Paladin", "subclass": "Oath of Devotion", "level": 5, "hit_die_type": "d10"}],
        "skills": [
            {"skill_name": "Athletics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Insight", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Persuasion", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Wisdom", "is_proficient": True, "bonus_override": None},
            {"ability": "Charisma", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "All armor, shields", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons, martial weapons", "notes": None},
        ],
        "features": [
            {"name": "Divine Smite", "source": "Paladin 2", "description": " expend spell slot to deal radiant damage.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Lay on Hands", "source": "Paladin 1", "description": "Pool of 25 HP to heal.", "uses_max": 25, "uses_current": 25, "recharge": "Long Rest"},
            {"name": "Extra Attack", "source": "Paladin 5", "description": "Attack twice instead of once.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Longsword", "attack_bonus": 6, "damage": "1d8+3", "damage_type": "Slashing", "properties": "Versatile (1d10)", "notes": None, "is_equipped": True},
            {"name": "Javelin", "attack_bonus": 6, "damage": "1d6+3", "damage_type": "Piercing", "properties": "Thrown (30/120)", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Chain mail", "equipment_type": "Armor", "description": "Heavy armor.", "quantity": 1, "weight": 55, "is_equipped": True, "armor_bonus": 16, "properties": "Disadvantage on Stealth"},
            {"name": "Holy Symbol", "equipment_type": "Adventuring Gear", "description": "Amulet of Bahamut.", "quantity": 1, "weight": 1, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Shield of Faith", "spell_level": 1, "school": "Abjuration", "casting_time": "1 bonus action", "range": "60 ft", "components": "V, S, M", "duration": "10 minutes", "description": "+2 AC to target.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": True},
            {"name": "Cure Wounds", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "Touch", "components": "V, S", "duration": "Instantaneous", "description": "Creature regains 1d8+spellcasting modifier HP.", "at_higher_levels": "+1d8 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Branding Smite", "spell_level": 2, "school": "Evocation", "casting_time": "1 bonus action", "range": "Self", "components": "V", "duration": "1 minute", "description": "Next weapon attack deals extra 2d6 radiant and reveals invisible.", "at_higher_levels": "+1d6 per slot above 2nd", "is_prepared": True, "is_ritual": False, "is_concentration": True},
        ],
        "notes": [{"title": "Dev Note", "content": "Paladin template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Brixby Tinkertop",
        "player_name": "Developer",
        "race": "Gnome",
        "subrace": "Forest Gnome",
        "alignment": "Chaotic Good",
        "background": "Entertainer",
        "experience_points": 900,
        "total_level": 3,
        "ability_scores": {"strength": 8, "dexterity": 14, "constitution": 12, "intelligence": 10, "wisdom": 12, "charisma": 16},
        "combat": {"max_hp": 22, "current_hp": 22, "temp_hp": 0, "armor_class": 14, "initiative_bonus": 2, "speed": 25, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": True, "proficiency_bonus": 2, "passive_perception": 11, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Charisma", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 4, "used": 0}, "2": {"max": 2, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 5, "ep": 0, "gp": 20, "pp": 0},
        "personality": {"personality_traits": "I know a story for every situation.", "ideals": "Art should be shared with the world.", "bonds": "I will do anything to prove myself greater than my siblings.", "flaws": "I have a hard time keeping secrets."},
        "appearance": {"age": "42", "height": "3'4\"", "weight": "40", "eyes": "Emerald", "skin": "Bronze", "hair": "Violet", "character_appearance": "Colorful clothing and a lute always on his back."},
        "background_details": {"backstory": "Traveled with a troupe of performers across Faerûn.", "allies_organizations": "The College of Valor", "additional_features_traits": "By Popular Demand", "treasure": ""},
        "classes": [{"class_name": "Bard", "subclass": "College of Lore", "level": 3, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Performance", "is_proficient": True, "is_expertise": True, "bonus_override": None},
            {"skill_name": "Persuasion", "is_proficient": True, "is_expertise": True, "bonus_override": None},
            {"skill_name": "Acrobatics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Dexterity", "is_proficient": True, "bonus_override": None},
            {"ability": "Charisma", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons, hand crossbows, longswords, rapiers, shortswords", "notes": None},
            {"proficiency_type": "Tools", "name": "Lute", "notes": None},
        ],
        "features": [
            {"name": "Bardic Inspiration", "source": "Bard 1", "description": "d6 to add to ally's roll.", "uses_max": 3, "uses_current": 3, "recharge": "Short Rest"},
            {"name": "Jack of All Trades", "source": "Bard 2", "description": "Add half proficiency to non-proficient skills.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Cutting Words", "source": "Bard 3", "description": "Subtract Bardic Inspiration die from enemy attack, damage, or ability check.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Rapier", "attack_bonus": 4, "damage": "1d8+2", "damage_type": "Piercing", "properties": "Finesse", "notes": None, "is_equipped": True},
            {"name": "Dagger", "attack_bonus": 4, "damage": "1d4+2", "damage_type": "Piercing", "properties": "Finesse, light, thrown (20/60)", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Leather armor", "equipment_type": "Armor", "description": "Light armor.", "quantity": 1, "weight": 10, "is_equipped": True, "armor_bonus": 11, "properties": None},
            {"name": "Lute", "equipment_type": "Tools", "description": "A finely crafted instrument.", "quantity": 1, "weight": 2, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Vicious Mockery", "spell_level": 0, "school": "Enchantment", "casting_time": "1 action", "range": "60 ft", "components": "V", "duration": "Instantaneous", "description": "Target takes 1d4 psychic damage and has disadvantage on next attack.", "at_higher_levels": "+1d4 at 5th, 11th, 17th level", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Healing Word", "spell_level": 1, "school": "Evocation", "casting_time": "1 bonus action", "range": "60 ft", "components": "V", "duration": "Instantaneous", "description": "Creature regains 1d4+spellcasting modifier HP.", "at_higher_levels": "+1d4 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Dissonant Whispers", "spell_level": 1, "school": "Enchantment", "casting_time": "1 action", "range": "60 ft", "components": "V", "duration": "Instantaneous", "description": "Target takes 3d6 psychic damage and must use reaction to flee.", "at_higher_levels": "+1d6 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
        ],
        "notes": [{"title": "Dev Note", "content": "Bard template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Kaelen Shadowstep",
        "player_name": "Developer",
        "race": "Half-Elf",
        "subrace": "",
        "alignment": "Neutral",
        "background": "Hermit",
        "experience_points": 2700,
        "total_level": 5,
        "ability_scores": {"strength": 12, "dexterity": 16, "constitution": 14, "intelligence": 10, "wisdom": 14, "charisma": 10},
        "combat": {"max_hp": 42, "current_hp": 42, "temp_hp": 0, "armor_class": 15, "initiative_bonus": 3, "speed": 30, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 3, "passive_perception": 14, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Wisdom", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 4, "used": 0}, "2": {"max": 3, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 15, "pp": 0},
        "personality": {"personality_traits": "I feel far more comfortable around animals than people.", "ideals": "Life is like the seasons, ever changing.", "bonds": "I am the last of my tribe, and it is up to me to ensure their names enter legend.", "flaws": "I am dogmatic in my thoughts and philosophy."},
        "appearance": {"age": "35", "height": "5'10\"", "weight": "160", "eyes": "Hazel", "skin": "Olive", "hair": "Black", "character_appearance": "Clad in weather-beaten leathers with a cloak of leaves."},
        "background_details": {"backstory": "Lived alone in the forests for years.", "allies_organizations": "The Emerald Enclave", "additional_features_traits": "Discovery", "treasure": ""},
        "classes": [{"class_name": "Ranger", "subclass": "Hunter", "level": 5, "hit_die_type": "d10"}],
        "skills": [
            {"skill_name": "Survival", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Nature", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Stealth", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Strength", "is_proficient": True, "bonus_override": None},
            {"ability": "Dexterity", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor, medium armor, shields", "notes": None},
            {"proficiency_type": "Weapons", "name": "Simple weapons, martial weapons", "notes": None},
        ],
        "features": [
            {"name": "Favored Enemy", "source": "Ranger 1", "description": "+2 bonus to recall information about beasts.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Colossus Slayer", "source": "Ranger 3", "description": "Extra 1d8 damage to wounded creatures once per turn.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Extra Attack", "source": "Ranger 5", "description": "Attack twice instead of once.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Longbow", "attack_bonus": 7, "damage": "1d8+3", "damage_type": "Piercing", "properties": "Ammunition, range 150/600, heavy, two-handed", "notes": None, "is_equipped": True},
            {"name": "Shortswords", "attack_bonus": 6, "damage": "1d6+3", "damage_type": "Piercing", "properties": "Finesse, light", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Studded leather", "equipment_type": "Armor", "description": "Light armor.", "quantity": 1, "weight": 13, "is_equipped": True, "armor_bonus": 12, "properties": None},
            {"name": "Quiver", "equipment_type": "Adventuring Gear", "description": "Holds 20 arrows.", "quantity": 1, "weight": 1, "is_equipped": False, "armor_bonus": None, "properties": None},
        ],
        "spells": [
            {"name": "Hunter's Mark", "spell_level": 1, "school": "Divination", "casting_time": "1 bonus action", "range": "90 ft", "components": "V", "duration": "1 hour", "description": "+1d6 damage to target, advantage on tracking it.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": True},
            {"name": "Cure Wounds", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "Touch", "components": "V, S", "duration": "Instantaneous", "description": "Creature regains 1d8+spellcasting modifier HP.", "at_higher_levels": "+1d8 per slot above 1nd", "is_prepared": True, "is_ritual": False, "is_concentration": False},
        ],
        "notes": [{"title": "Dev Note", "content": "Ranger template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Mira Willowbrook",
        "player_name": "Developer",
        "race": "Elf",
        "subrace": "Wood Elf",
        "alignment": "Neutral Good",
        "background": "Outlander",
        "experience_points": 900,
        "total_level": 3,
        "ability_scores": {"strength": 10, "dexterity": 14, "constitution": 13, "intelligence": 12, "wisdom": 16, "charisma": 10},
        "combat": {"max_hp": 24, "current_hp": 24, "temp_hp": 0, "armor_class": 15, "initiative_bonus": 2, "speed": 35, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 15, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Wisdom", "spell_save_dc": 13, "spell_attack_bonus": 5, "spell_slots": {"1": {"max": 4, "used": 0}, "2": {"max": 2, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 10, "pp": 0},
        "personality": {"personality_traits": "I watch over my friends as if they were a litter of newborn pups.", "ideals": "The natural world is more important than any civilization.", "bonds": "I suffer awful visions of a coming disaster and will do anything to prevent it.", "flaws": "Don't expect me to save those who can't save themselves."},
        "appearance": {"age": "142", "height": "5'4\"", "weight": "115", "eyes": "Green", "skin": "Copper", "hair": "Red", "character_appearance": "Wears hide armor made from natural materials and carries a wooden shield."},
        "background_details": {"backstory": "Raised deep in the ancient forests.", "allies_organizations": "The Circle of the Moon", "additional_features_traits": "Wanderer", "treasure": ""},
        "classes": [{"class_name": "Druid", "subclass": "Circle of the Moon", "level": 3, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Nature", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Survival", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Animal Handling", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Intelligence", "is_proficient": True, "bonus_override": None},
            {"ability": "Wisdom", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Armor", "name": "Light armor, medium armor, shields", "notes": None},
            {"proficiency_type": "Weapons", "name": "Clubs, daggers, darts, javelins, maces, quarterstaffs, scimitars, sickles", "notes": None},
        ],
        "features": [
            {"name": "Wild Shape", "source": "Druid 2", "description": "Transform into beasts with CR 1/4 or lower.", "uses_max": 2, "uses_current": 2, "recharge": "Short Rest"},
            {"name": "Combat Wild Shape", "source": "Druid 2", "description": "Bonus action to transform and heal as bonus action.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Scimitar", "attack_bonus": 4, "damage": "1d6+2", "damage_type": "Slashing", "properties": "Finesse, light", "notes": None, "is_equipped": True},
            {"name": "Sling", "attack_bonus": 4, "damage": "1d4+2", "damage_type": "Bludgeoning", "properties": "Ammunition, range 30/120", "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Hide armor", "equipment_type": "Armor", "description": "Medium armor.", "quantity": 1, "weight": 12, "is_equipped": True, "armor_bonus": 12, "properties": None},
            {"name": "Wooden Shield", "equipment_type": "Armor", "description": "+2 AC when wielded.", "quantity": 1, "weight": 6, "is_equipped": True, "armor_bonus": 2, "properties": None},
        ],
        "spells": [
            {"name": "Thunderwave", "spell_level": 1, "school": "Evocation", "casting_time": "1 action", "range": "Self (15 ft cube)", "components": "V, S", "duration": "Instantaneous", "description": "Each creature in area takes 2d8 thunder damage and pushed 10 ft.", "at_higher_levels": "+1d8 per slot above 1st", "is_prepared": True, "is_ritual": False, "is_concentration": False},
            {"name": "Entangle", "spell_level": 1, "school": "Conjuration", "casting_time": "1 action", "range": "90 ft", "components": "V, S", "duration": "1 minute", "description": "Plants restrain creatures in 20 ft square.", "at_higher_levels": None, "is_prepared": True, "is_ritual": False, "is_concentration": True},
            {"name": "Moonbeam", "spell_level": 2, "school": "Evocation", "casting_time": "1 action", "range": "120 ft", "components": "V, S, M", "duration": "1 minute", "description": "Creatures in cylinder take 2d10 radiant damage.", "at_higher_levels": "+1d10 per slot above 2nd", "is_prepared": True, "is_ritual": False, "is_concentration": True},
        ],
        "notes": [{"title": "Dev Note", "content": "Druid template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
    {
        "name": "Kai Swiftstrike",
        "player_name": "Developer",
        "race": "Human",
        "subrace": "Variant",
        "alignment": "Lawful Neutral",
        "background": "Hermit",
        "experience_points": 650,
        "total_level": 2,
        "ability_scores": {"strength": 12, "dexterity": 16, "constitution": 14, "intelligence": 10, "wisdom": 14, "charisma": 10},
        "combat": {"max_hp": 18, "current_hp": 18, "temp_hp": 0, "armor_class": 16, "initiative_bonus": 3, "speed": 45, "death_save_successes": 0, "death_save_failures": 0},
        "general": {"inspiration": False, "proficiency_bonus": 2, "passive_perception": 12, "exhaustion_level": 0, "encumbrance_status": "normal"},
        "spellcasting": {"spellcasting_ability": "Wisdom", "spell_save_dc": 12, "spell_attack_bonus": 4, "spell_slots": {"1": {"max": 2, "used": 0}, "2": {"max": 0, "used": 0}, "3": {"max": 0, "used": 0}, "4": {"max": 0, "used": 0}, "5": {"max": 0, "used": 0}, "6": {"max": 0, "used": 0}, "7": {"max": 0, "used": 0}, "8": {"max": 0, "used": 0}, "9": {"max": 0, "used": 0}}},
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 12, "pp": 0},
        "personality": {"personality_traits": "I am always calm, no matter what the situation.", "ideals": "Discipline is the path to mastery.", "bonds": "My monastery is my true home.", "flaws": "I am obsessed with becoming the perfect warrior."},
        "appearance": {"age": "21", "height": "5'8\"", "weight": "150", "eyes": "Gray", "skin": "Tan", "hair": "Black", "character_appearance": "Wears simple robes and moves with fluid grace."},
        "background_details": {"backstory": "Trained in a remote mountain monastery.", "allies_organizations": "The Order of the Empty Hand", "additional_features_traits": "Discovery", "treasure": ""},
        "classes": [{"class_name": "Monk", "subclass": "Way of the Open Hand", "level": 2, "hit_die_type": "d8"}],
        "skills": [
            {"skill_name": "Acrobatics", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Insight", "is_proficient": True, "is_expertise": False, "bonus_override": None},
            {"skill_name": "Stealth", "is_proficient": True, "is_expertise": False, "bonus_override": None},
        ],
        "saving_throws": [
            {"ability": "Strength", "is_proficient": True, "bonus_override": None},
            {"ability": "Dexterity", "is_proficient": True, "bonus_override": None},
        ],
        "proficiencies": [
            {"proficiency_type": "Weapons", "name": "Simple weapons, shortswords", "notes": None},
            {"proficiency_type": "Tools", "name": "One type of artisan's tools or musical instrument", "notes": None},
        ],
        "features": [
            {"name": "Martial Arts", "source": "Monk 1", "description": "Unarmed strikes deal d4+Dex damage and bonus action attack.", "uses_max": None, "uses_current": None, "recharge": None},
            {"name": "Ki", "source": "Monk 2", "description": "Spend ki points for Flurry of Blows, Patient Defense, or Step of the Wind.", "uses_max": 2, "uses_current": 2, "recharge": "Short Rest"},
            {"name": "Unarmored Movement", "source": "Monk 2", "description": "Speed increases by 10 ft when not wearing armor or shield.", "uses_max": None, "uses_current": None, "recharge": None},
        ],
        "weapons": [
            {"name": "Shortsword", "attack_bonus": 5, "damage": "1d6+3", "damage_type": "Piercing", "properties": "Finesse, light", "notes": None, "is_equipped": True},
            {"name": "Unarmed Strike", "attack_bonus": 5, "damage": "1d4+3", "damage_type": "Bludgeoning", "properties": None, "notes": None, "is_equipped": True},
        ],
        "equipment": [
            {"name": "Explorer's Pack", "equipment_type": "Adventuring Gear", "description": "Basic adventuring supplies.", "quantity": 1, "weight": None, "is_equipped": False, "armor_bonus": None, "properties": None},
            {"name": "Dart", "equipment_type": "Weapon", "description": "Thrown weapon.", "quantity": 10, "weight": 0.25, "is_equipped": False, "armor_bonus": None, "properties": "Finesse, thrown (20/60)"},
        ],
        "spells": [],
        "notes": [{"title": "Dev Note", "content": "Monk template generated by /dev/character."}],
        "resources": [],
        "companions": [],
        "conditions": [],
    },
]


@app.route('/api/dev/character', methods=['POST'])
@token_required
def create_dev_character(current_user):
    template = random.choice(_DEV_CHARACTER_TEMPLATES)
    character = Character(user_id=current_user.id)
    _build_character_from_data(character, template)

    db.session.add(character)
    db.session.flush()
    _update_character_relations(character, template)
    db.session.commit()

    return jsonify({'message': 'Dev character created', 'character': _character_full_dict(character)}), 201


# ---------------------------------------------------------------------------
# Campaign Dashboard API Endpoints
# ---------------------------------------------------------------------------

def _generate_invite_code():
    import secrets
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def _ensure_member(campaign, user):
    """Ensure user is a member of the campaign (owner auto-included)."""
    if campaign.user_id == user.id:
        return True
    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=user.id).first()
    return member is not None


# -- Campaign Characters --

@app.route('/api/campaigns/<int:campaign_id>/characters', methods=['GET'])
@token_required
def get_campaign_characters(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    characters = Character.query.filter_by(campaign_id=campaign_id).order_by(Character.party_order).all()
    return jsonify({'characters': [_character_full_dict(c) for c in characters]}), 200


@app.route('/api/campaigns/<int:campaign_id>/characters', methods=['POST'])
@token_required
def add_campaign_character(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    if not data or not data.get('character_id'):
        return jsonify({'error': 'Missing character_id'}), 400
    character = Character.query.get_or_404(data['character_id'])
    if character.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    character.campaign_id = campaign_id
    db.session.commit()
    return jsonify({'character': _character_full_dict(character)}), 200


# -- Sessions --

@app.route('/api/campaigns/<int:campaign_id>/sessions', methods=['POST'])
@token_required
def start_session(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    active = CampaignSession.query.filter_by(campaign_id=campaign_id, is_active=True).first()
    if active:
        return jsonify({'error': 'An active session already exists'}), 400
    session = CampaignSession(campaign_id=campaign_id)
    db.session.add(session)
    campaign.last_played_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'session': session.to_dict()}), 201


@app.route('/api/campaigns/<int:campaign_id>/sessions', methods=['GET'])
@token_required
def list_sessions(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    sessions = CampaignSession.query.filter_by(campaign_id=campaign_id).order_by(CampaignSession.started_at.desc()).all()
    return jsonify({'sessions': [s.to_dict() for s in sessions]}), 200


@app.route('/api/sessions/<int:session_id>', methods=['GET'])
@token_required
def get_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    data = session.to_dict()
    data['messages'] = [m.to_dict() for m in session.messages]
    return jsonify({'session': data}), 200


@app.route('/api/sessions/<int:session_id>', methods=['PUT'])
@token_required
def end_session(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    session.is_active = False
    session.ended_at = datetime.utcnow()
    if data and 'recap' in data:
        session.recap = data['recap']
    db.session.commit()
    return jsonify({'session': session.to_dict()}), 200


# -- Messages --

@app.route('/api/sessions/<int:session_id>/messages', methods=['GET'])
@token_required
def get_messages(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    messages = SessionMessage.query.filter_by(session_id=session_id).order_by(SessionMessage.created_at).all()
    return jsonify({'messages': [m.to_dict() for m in messages]}), 200


@app.route('/api/sessions/<int:session_id>/messages', methods=['POST'])
@token_required
def send_message(current_user, session_id):
    session = CampaignSession.query.get_or_404(session_id)
    campaign = Campaign.query.get(session.campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'Missing content'}), 400
    msg = SessionMessage(
        session_id=session_id,
        role=data.get('role', 'player'),
        content=data['content'],
    )
    db.session.add(msg)
    db.session.commit()
    result_messages = [msg.to_dict()]

    ai_text = get_dm_response(session.messages)
    if ai_text:
        ai_msg = SessionMessage(
            session_id=session_id,
            role='dm',
            content=ai_text,
        )
        db.session.add(ai_msg)
        db.session.commit()
        result_messages.append(ai_msg.to_dict())

    return jsonify({'messages': result_messages}), 201


# -- Members & Invites --

@app.route('/api/campaigns/<int:campaign_id>/members', methods=['GET'])
@token_required
def list_members(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if not _ensure_member(campaign, current_user):
        return jsonify({'error': 'Forbidden'}), 403
    members = CampaignMember.query.filter_by(campaign_id=campaign_id).all()
    return jsonify({'members': [m.to_dict() for m in members]}), 200


@app.route('/api/campaigns/<int:campaign_id>/invites', methods=['POST'])
@token_required
def create_invite(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Only the campaign owner can create invites'}), 403
    code = _generate_invite_code()
    invite = CampaignInvite(
        campaign_id=campaign_id,
        code=code,
        created_by=current_user.id,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.session.add(invite)
    campaign.invite_code = code
    db.session.commit()
    return jsonify({'invite': invite.to_dict()}), 201


@app.route('/api/campaigns/<int:campaign_id>/join', methods=['POST'])
@token_required
def join_campaign(current_user, campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    data = request.get_json()
    if not data or not data.get('code'):
        return jsonify({'error': 'Missing invite code'}), 400
    if campaign.invite_code != data['code']:
        return jsonify({'error': 'Invalid invite code'}), 403
    existing = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=current_user.id).first()
    if existing:
        return jsonify({'error': 'Already a member'}), 400
    member = CampaignMember(campaign_id=campaign_id, user_id=current_user.id, role='player')
    db.session.add(member)
    db.session.commit()
    return jsonify({'member': member.to_dict()}), 201


@app.route('/api/campaigns/<int:campaign_id>/members/<int:user_id>', methods=['PUT'])
@token_required
def update_member_role(current_user, campaign_id, user_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id:
        return jsonify({'error': 'Forbidden'}), 403
    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first_or_404()
    data = request.get_json()
    if data and 'role' in data:
        member.role = data['role']
    db.session.commit()
    return jsonify({'member': member.to_dict()}), 200


@app.route('/api/campaigns/<int:campaign_id>/members/<int:user_id>', methods=['DELETE'])
@token_required
def remove_member(current_user, campaign_id, user_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.user_id != current_user.id and current_user.id != user_id:
        return jsonify({'error': 'Forbidden'}), 403
    member = CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first_or_404()
    db.session.delete(member)
    db.session.commit()
    return jsonify({'message': 'Member removed'}), 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5889)