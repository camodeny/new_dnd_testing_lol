from datetime import datetime
import json

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    sso_subject = db.Column(db.String(160), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    campaigns = db.relationship('Campaign', backref='owner', lazy=True)
    characters = db.relationship('Character', backref='player', lazy=True)
    llm_player_profile = db.relationship('LLMPlayer', backref='user', uselist=False, lazy=True, cascade='all, delete-orphan')
    auth_sessions = db.relationship('AuthSession', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        llm_profile = self.llm_player_profile
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'sso_subject': self.sso_subject,
            'created_at': self.created_at.isoformat(),
            'llm_player': llm_profile.to_dict() if llm_profile else None,
        }


class AuthSession(db.Model):
    __tablename__ = 'auth_sessions'

    id = db.Column(db.Integer, primary_key=True)
    session_token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    provider = db.Column(db.String(40), nullable=True)
    provider_subject = db.Column(db.String(160), nullable=True, index=True)
    access_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)
    id_token = db.Column(db.Text, nullable=True)
    scope = db.Column(db.String(200), nullable=True)
    provider_user_json = db.Column(db.Text, nullable=True)
    access_token_expires_at = db.Column(db.DateTime, nullable=True)
    pending_oauth_state = db.Column(db.String(120), nullable=True, index=True)
    pending_oauth_state_expires_at = db.Column(db.DateTime, nullable=True)
    post_login_redirect = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Campaign(db.Model):
    __tablename__ = 'campaign'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    description = db.Column(db.String, nullable=True)
    difficulty = db.Column(db.String, nullable=True)
    seed = db.Column(db.String, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String, default='active')
    last_played_at = db.Column(db.DateTime, nullable=True)
    settings = db.Column(db.Text, nullable=True)
    invite_code = db.Column(db.String(20), nullable=True, unique=True)
    characters = db.relationship('Character', backref='campaign', lazy=True)
    sessions = db.relationship('CampaignSession', backref='campaign', lazy=True, cascade='all, delete-orphan')
    members = db.relationship('CampaignMember', backref='campaign', lazy=True, cascade='all, delete-orphan')
    invites = db.relationship('CampaignInvite', backref='campaign', lazy=True, cascade='all, delete-orphan')
    world = db.relationship('CampaignWorld', backref='campaign', lazy=True, cascade='all, delete-orphan', uselist=False)
    npc_actors = db.relationship('NPCActor', backref='campaign', lazy=True, cascade='all, delete-orphan')
    clocks = db.relationship('CampaignClock', backref='campaign', lazy=True, cascade='all, delete-orphan')
    world_events = db.relationship('WorldEvent', backref='campaign', lazy=True, cascade='all, delete-orphan')
    memory_embeddings = db.relationship('CampaignMemoryEmbedding', backref='campaign', lazy=True, cascade='all, delete-orphan')
    encounter_maps = db.relationship('EncounterMap', backref='campaign', lazy=True, cascade='all, delete-orphan')
    monsters = db.relationship('CampaignMonster', backref='campaign', lazy=True, cascade='all, delete-orphan')


    def to_dict(self):
        import json
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'difficulty': self.difficulty,
            'seed': self.seed,
            'user_id': self.user_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'status': self.status,
            'last_played_at': self.last_played_at.isoformat() if self.last_played_at else None,
            'settings': json.loads(self.settings) if self.settings else {},
            'invite_code': self.invite_code,
        }


class Character(db.Model):
    __tablename__ = 'characters'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=True)

    # Basic Info
    name = db.Column(db.String(100), nullable=False)
    player_name = db.Column(db.String(100), nullable=True)
    race = db.Column(db.String(50), nullable=False)
    subrace = db.Column(db.String(50), nullable=True)
    alignment = db.Column(db.String(20), nullable=True)
    background = db.Column(db.String(50), nullable=True)
    experience_points = db.Column(db.Integer, default=0)
    total_level = db.Column(db.Integer, default=1)

    # Ability Scores
    strength = db.Column(db.Integer, default=10)
    dexterity = db.Column(db.Integer, default=10)
    constitution = db.Column(db.Integer, default=10)
    intelligence = db.Column(db.Integer, default=10)
    wisdom = db.Column(db.Integer, default=10)
    charisma = db.Column(db.Integer, default=10)

    # Combat
    max_hp = db.Column(db.Integer, default=1)
    current_hp = db.Column(db.Integer, default=1)
    temp_hp = db.Column(db.Integer, default=0)
    armor_class = db.Column(db.Integer, default=10)
    initiative_bonus = db.Column(db.Integer, default=0)
    speed = db.Column(db.Integer, default=30)
   
    # Death Saves
    death_save_successes = db.Column(db.Integer, default=0)
    death_save_failures = db.Column(db.Integer, default=0)

    # General
    inspiration = db.Column(db.Boolean, default=False)
    proficiency_bonus = db.Column(db.Integer, default=2)
    passive_perception = db.Column(db.Integer, default=10)
    exhaustion_level = db.Column(db.Integer, default=0)

    # Spellcasting Summary
    spellcasting_ability = db.Column(db.String(10), nullable=True)
    spell_save_dc = db.Column(db.Integer, nullable=True)
    spell_attack_bonus = db.Column(db.Integer, nullable=True)

    # Spell Slots (max available)
    spell_slots_level_1 = db.Column(db.Integer, default=0)
    spell_slots_level_2 = db.Column(db.Integer, default=0)
    spell_slots_level_3 = db.Column(db.Integer, default=0)
    spell_slots_level_4 = db.Column(db.Integer, default=0)
    spell_slots_level_5 = db.Column(db.Integer, default=0)
    spell_slots_level_6 = db.Column(db.Integer, default=0)
    spell_slots_level_7 = db.Column(db.Integer, default=0)
    spell_slots_level_8 = db.Column(db.Integer, default=0)
    spell_slots_level_9 = db.Column(db.Integer, default=0)

    # Spell Slots Used
    spell_slots_used_1 = db.Column(db.Integer, default=0)
    spell_slots_used_2 = db.Column(db.Integer, default=0)
    spell_slots_used_3 = db.Column(db.Integer, default=0)
    spell_slots_used_4 = db.Column(db.Integer, default=0)
    spell_slots_used_5 = db.Column(db.Integer, default=0)
    spell_slots_used_6 = db.Column(db.Integer, default=0)
    spell_slots_used_7 = db.Column(db.Integer, default=0)
    spell_slots_used_8 = db.Column(db.Integer, default=0)
    spell_slots_used_9 = db.Column(db.Integer, default=0)

    # Currency
    cp = db.Column(db.Integer, default=0)
    sp = db.Column(db.Integer, default=0)
    ep = db.Column(db.Integer, default=0)
    gp = db.Column(db.Integer, default=0)
    pp = db.Column(db.Integer, default=0)

    # Personality
    personality_traits = db.Column(db.Text, nullable=True)
    ideals = db.Column(db.Text, nullable=True)
    bonds = db.Column(db.Text, nullable=True)
    flaws = db.Column(db.Text, nullable=True)

    # Appearance
    age = db.Column(db.String(20), nullable=True)
    height = db.Column(db.String(20), nullable=True)
    weight = db.Column(db.String(20), nullable=True)
    eyes = db.Column(db.String(50), nullable=True)
    skin = db.Column(db.String(50), nullable=True)
    hair = db.Column(db.String(50), nullable=True)
    character_appearance = db.Column(db.Text, nullable=True)

    # Background Details
    backstory = db.Column(db.Text, nullable=True)
    allies_organizations = db.Column(db.Text, nullable=True)
    additional_features_traits = db.Column(db.Text, nullable=True)
    treasure = db.Column(db.Text, nullable=True)

    # Encumbrance / Carry
    encumbrance_status = db.Column(db.String(20), default='normal')  # normal, encumbered, heavily_encumbered

    # Campaign Dashboard
    current_location = db.Column(db.String(200), nullable=True)
    party_order = db.Column(db.Integer, default=0)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    classes = db.relationship('CharacterClass', backref='character', lazy=True, cascade='all, delete-orphan')
    skills = db.relationship('CharacterSkill', backref='character', lazy=True, cascade='all, delete-orphan')
    saving_throws = db.relationship('CharacterSavingThrow', backref='character', lazy=True, cascade='all, delete-orphan')
    proficiencies = db.relationship('CharacterProficiency', backref='character', lazy=True, cascade='all, delete-orphan')
    features = db.relationship('CharacterFeature', backref='character', lazy=True, cascade='all, delete-orphan')
    weapons = db.relationship('CharacterWeapon', backref='character', lazy=True, cascade='all, delete-orphan')
    equipment = db.relationship('CharacterEquipment', backref='character', lazy=True, cascade='all, delete-orphan')
    spells = db.relationship('CharacterSpell', backref='character', lazy=True, cascade='all, delete-orphan')
    notes = db.relationship('CharacterNote', backref='character', lazy=True, cascade='all, delete-orphan')
    resources = db.relationship('CharacterResource', backref='character', lazy=True, cascade='all, delete-orphan')
    companions = db.relationship('CharacterCompanion', backref='character', lazy=True, cascade='all, delete-orphan')
    conditions = db.relationship('CharacterCondition', backref='character', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'campaign_id': self.campaign_id,
            'name': self.name,
            'player_name': self.player_name,
            'race': self.race,
            'subrace': self.subrace,
            'alignment': self.alignment,
            'background': self.background,
            'experience_points': self.experience_points,
            'total_level': self.total_level,
            'ability_scores': {
                'strength': self.strength,
                'dexterity': self.dexterity,
                'constitution': self.constitution,
                'intelligence': self.intelligence,
                'wisdom': self.wisdom,
                'charisma': self.charisma,
            },
            'combat': {
                'max_hp': self.max_hp,
                'current_hp': self.current_hp,
                'temp_hp': self.temp_hp,
                'armor_class': self.armor_class,
                'initiative_bonus': self.initiative_bonus,
                'speed': self.speed,
                'death_save_successes': self.death_save_successes,
                'death_save_failures': self.death_save_failures,
            },
            'general': {
                'inspiration': self.inspiration,
                'proficiency_bonus': self.proficiency_bonus,
                'passive_perception': self.passive_perception,
                'exhaustion_level': self.exhaustion_level,
                'encumbrance_status': self.encumbrance_status,
            },
            'spellcasting': {
                'spellcasting_ability': self.spellcasting_ability,
                'spell_save_dc': self.spell_save_dc,
                'spell_attack_bonus': self.spell_attack_bonus,
                'spell_slots': {
                    str(i): {
                        'max': getattr(self, f'spell_slots_level_{i}'),
                        'used': getattr(self, f'spell_slots_used_{i}'),
                    }
                    for i in range(1, 10)
                }
            },
            'currency': {
                'cp': self.cp,
                'sp': self.sp,
                'ep': self.ep,
                'gp': self.gp,
                'pp': self.pp,
            },
            'personality': {
                'personality_traits': self.personality_traits,
                'ideals': self.ideals,
                'bonds': self.bonds,
                'flaws': self.flaws,
            },
            'appearance': {
                'age': self.age,
                'height': self.height,
                'weight': self.weight,
                'eyes': self.eyes,
                'skin': self.skin,
                'hair': self.hair,
                'character_appearance': self.character_appearance,
            },
            'background_details': {
                'backstory': self.backstory,
                'allies_organizations': self.allies_organizations,
                'additional_features_traits': self.additional_features_traits,
                'treasure': self.treasure,
            },
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CharacterClass(db.Model):
    __tablename__ = 'character_classes'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    class_name = db.Column(db.String(50), nullable=False)
    subclass = db.Column(db.String(50), nullable=True)
    level = db.Column(db.Integer, default=1)
    hit_die_type = db.Column(db.String(10), default='d8')

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'class_name': self.class_name,
            'subclass': self.subclass,
            'level': self.level,
            'hit_die_type': self.hit_die_type,
        }


class CharacterSkill(db.Model):
    __tablename__ = 'character_skills'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    skill_name = db.Column(db.String(50), nullable=False)
    is_proficient = db.Column(db.Boolean, default=False)
    is_expertise = db.Column(db.Boolean, default=False)
    bonus_override = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'skill_name': self.skill_name,
            'is_proficient': self.is_proficient,
            'is_expertise': self.is_expertise,
            'bonus_override': self.bonus_override,
        }


class CharacterSavingThrow(db.Model):
    __tablename__ = 'character_saving_throws'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    ability = db.Column(db.String(20), nullable=False)
    is_proficient = db.Column(db.Boolean, default=False)
    bonus_override = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'ability': self.ability,
            'is_proficient': self.is_proficient,
            'bonus_override': self.bonus_override,
        }


class CharacterProficiency(db.Model):
    __tablename__ = 'character_proficiencies'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    proficiency_type = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    notes = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'proficiency_type': self.proficiency_type,
            'name': self.name,
            'notes': self.notes,
        }


class CharacterFeature(db.Model):
    __tablename__ = 'character_features'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    source = db.Column(db.String(100), nullable=True)
    description = db.Column(db.Text, nullable=True)
    uses_max = db.Column(db.Integer, nullable=True)
    uses_current = db.Column(db.Integer, nullable=True)
    recharge = db.Column(db.String(50), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'source': self.source,
            'description': self.description,
            'uses_max': self.uses_max,
            'uses_current': self.uses_current,
            'recharge': self.recharge,
        }


class CharacterWeapon(db.Model):
    __tablename__ = 'character_weapons'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    attack_bonus = db.Column(db.Integer, default=0)
    damage = db.Column(db.String(50), nullable=True)
    damage_type = db.Column(db.String(50), nullable=True)
    properties = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_equipped = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'attack_bonus': self.attack_bonus,
            'damage': self.damage,
            'damage_type': self.damage_type,
            'properties': self.properties,
            'notes': self.notes,
            'is_equipped': self.is_equipped,
        }


class CharacterEquipment(db.Model):
    __tablename__ = 'character_equipment'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    equipment_type = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Integer, default=1)
    weight = db.Column(db.Float, nullable=True)
    is_equipped = db.Column(db.Boolean, default=False)
    armor_bonus = db.Column(db.Integer, nullable=True)
    properties = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'equipment_type': self.equipment_type,
            'description': self.description,
            'quantity': self.quantity,
            'weight': self.weight,
            'is_equipped': self.is_equipped,
            'armor_bonus': self.armor_bonus,
            'properties': self.properties,
        }


class CharacterSpell(db.Model):
    __tablename__ = 'character_spells'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    spell_level = db.Column(db.Integer, default=0)
    school = db.Column(db.String(50), nullable=True)
    casting_time = db.Column(db.String(50), nullable=True)
    range_str = db.Column(db.String(50), nullable=True)
    components = db.Column(db.String(100), nullable=True)
    duration = db.Column(db.String(100), nullable=True)
    description = db.Column(db.Text, nullable=True)
    at_higher_levels = db.Column(db.Text, nullable=True)
    is_prepared = db.Column(db.Boolean, default=False)
    is_ritual = db.Column(db.Boolean, default=False)
    is_concentration = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'spell_level': self.spell_level,
            'school': self.school,
            'casting_time': self.casting_time,
            'range': self.range_str,
            'components': self.components,
            'duration': self.duration,
            'description': self.description,
            'at_higher_levels': self.at_higher_levels,
            'is_prepared': self.is_prepared,
            'is_ritual': self.is_ritual,
            'is_concentration': self.is_concentration,
        }


class CharacterNote(db.Model):
    __tablename__ = 'character_notes'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    title = db.Column(db.String(200), nullable=True)
    content = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'title': self.title,
            'content': self.content,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CharacterResource(db.Model):
    __tablename__ = 'character_resources'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    current = db.Column(db.Integer, default=0)
    max_amount = db.Column(db.Integer, default=0)
    recharge = db.Column(db.String(50), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'current': self.current,
            'max': self.max_amount,
            'recharge': self.recharge,
        }


class CharacterCompanion(db.Model):
    __tablename__ = 'character_companions'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    companion_type = db.Column(db.String(100), nullable=True)
    max_hp = db.Column(db.Integer, default=1)
    current_hp = db.Column(db.Integer, default=1)
    armor_class = db.Column(db.Integer, nullable=True)
    speed = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'name': self.name,
            'companion_type': self.companion_type,
            'max_hp': self.max_hp,
            'current_hp': self.current_hp,
            'armor_class': self.armor_class,
            'speed': self.speed,
            'description': self.description,
            'notes': self.notes,
        }


class CharacterCondition(db.Model):
    __tablename__ = 'character_conditions'

    id = db.Column(db.Integer, primary_key=True)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    condition_name = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(100), nullable=True)
    is_permanent = db.Column(db.Boolean, default=False)
    duration_remaining = db.Column(db.String(50), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'character_id': self.character_id,
            'condition_name': self.condition_name,
            'description': self.description,
            'source': self.source,
            'is_permanent': self.is_permanent,
            'duration_remaining': self.duration_remaining,
        }


# ---------------------------------------------------------------------------
# Campaign Dashboard Models
# ---------------------------------------------------------------------------

class CampaignSession(db.Model):
    __tablename__ = 'campaign_sessions'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)
    recap = db.Column(db.Text, nullable=True)
    running_summary = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    messages = db.relationship('SessionMessage', backref='session', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None,
            'recap': self.recap,
            'running_summary': self.running_summary,
            'is_active': self.is_active,
        }


class SessionMessage(db.Model):
    __tablename__ = 'session_messages'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User')

    def to_dict(self):
        llm_profile = self.user.llm_player_profile if self.user else None
        return {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'username': llm_profile.label if llm_profile else (self.user.username if self.user else None),
            'role': self.role,
            'content': self.content,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CampaignMember(db.Model):
    __tablename__ = 'campaign_members'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), default='player')
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    selected_character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=True)
    character_ready_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User')
    selected_character = db.relationship('Character', foreign_keys=[selected_character_id])

    def to_dict(self):
        llm_profile = self.user.llm_player_profile if self.user else None
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'user_id': self.user_id,
            'username': llm_profile.label if llm_profile else (self.user.username if self.user else None),
            'role': self.role,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None,
            'selected_character_id': self.selected_character_id,
            'character_ready_at': self.character_ready_at.isoformat() if self.character_ready_at else None,
            'is_character_ready': self.character_ready_at is not None and self.selected_character_id is not None,
            'is_llm_player': bool(llm_profile),
            'llm_player_id': llm_profile.id if llm_profile else None,
        }


class LLMPlayer(db.Model):
    __tablename__ = 'llm_players'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    label = db.Column(db.String(120), nullable=False)
    api_key_hash = db.Column(db.String(256), nullable=False)
    api_key_prefix = db.Column(db.String(24), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    campaign = db.relationship('Campaign')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'user_id': self.user_id,
            'label': self.label,
            'api_key_prefix': self.api_key_prefix,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
        }


class CampaignInvite(db.Model):
    __tablename__ = 'campaign_invites'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_used = db.Column(db.Boolean, default=False)
    creator = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'code': self.code,
            'created_by': self.created_by,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_used': self.is_used,
        }


class CharacterPlanningMessage(db.Model):
    __tablename__ = 'character_planning_messages'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    campaign = db.relationship('Campaign')
    user = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'user_id': self.user_id,
            'role': self.role,
            'content': self.content,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CampaignPlanningSummary(db.Model):
    __tablename__ = 'campaign_planning_summaries'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, unique=True)
    party_balance = db.Column(db.Text, nullable=True)
    confirmed_public_facts = db.Column(db.Text, nullable=True)
    dm_private_secrets = db.Column(db.Text, nullable=True)
    explicit_player_points = db.Column(db.Text, nullable=True)
    unresolved_gaps = db.Column(db.Text, nullable=True)
    accepted_hooks = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    campaign = db.relationship('Campaign')

    def to_dict(self, include_private=False, current_user_id=None):
        import json

        def loads(value, fallback):
            if not value:
                return fallback
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return fallback

        secrets = loads(self.dm_private_secrets, {})
        if not include_private:
            current_key = str(current_user_id) if current_user_id is not None else None
            secrets = {current_key: secrets.get(current_key, [])} if current_key and current_key in secrets else {}

        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'party_balance': loads(self.party_balance, ''),
            'confirmed_public_facts': loads(self.confirmed_public_facts, []),
            'dm_private_secrets': secrets,
            'explicit_player_points': loads(self.explicit_player_points, {}),
            'unresolved_gaps': loads(self.unresolved_gaps, []),
            'accepted_hooks': loads(self.accepted_hooks, []),
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class PlanningBondProposal(db.Model):
    __tablename__ = 'planning_bond_proposals'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    involved_user_ids = db.Column(db.Text, nullable=False)
    approval_states = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    campaign = db.relationship('Campaign')

    def to_dict(self):
        import json

        def loads(value, fallback):
            if not value:
                return fallback
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return fallback

        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'title': self.title,
            'description': self.description,
            'involved_user_ids': loads(self.involved_user_ids, []),
            'approval_states': loads(self.approval_states, {}),
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignWorld(db.Model):
    __tablename__ = 'campaign_worlds'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, unique=True)
    public_intro = db.Column(db.Text, nullable=False)
    knowledge_graph = db.Column(db.Text, nullable=False)
    world_state = db.Column(db.Text, nullable=False)
    dm_private = db.Column(db.Text, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_public_dict(self):
        import json

        try:
            public_intro = json.loads(self.public_intro)
        except (TypeError, ValueError):
            public_intro = {}

        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'public_intro': public_intro,
            'is_ready': True,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class NPCActor(db.Model):
    __tablename__ = 'npc_actors'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    actor_id = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(200), nullable=True)
    public_summary = db.Column(db.Text, nullable=True)
    dossier = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('campaign_id', 'actor_id', name='uq_npc_actor_campaign_actor'),
    )

    def to_dict(self, include_private=False):
        import json

        data = {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'actor_id': self.actor_id,
            'name': self.name,
            'role': self.role,
            'public_summary': self.public_summary,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_private:
            try:
                data['dossier'] = json.loads(self.dossier)
            except (TypeError, ValueError):
                data['dossier'] = {}
        return data


class CampaignClock(db.Model):
    __tablename__ = 'campaign_clocks'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    clock_id = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    segments = db.Column(db.Integer, default=4)
    filled = db.Column(db.Integer, default=0)
    pressure_type = db.Column(db.String(80), nullable=True)
    visibility = db.Column(db.String(30), default='dm_private')
    summary = db.Column(db.Text, nullable=True)
    trigger = db.Column(db.Text, nullable=True)
    on_complete = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('campaign_id', 'clock_id', name='uq_campaign_clock_campaign_clock'),
    )

    def to_dict(self, include_private=False):
        if self.visibility == 'dm_private' and not include_private:
            return None
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'clock_id': self.clock_id,
            'name': self.name,
            'segments': self.segments,
            'filled': self.filled,
            'pressure_type': self.pressure_type,
            'visibility': self.visibility,
            'summary': self.summary,
            'trigger': self.trigger if include_private else None,
            'on_complete': self.on_complete if include_private else None,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class WorldEvent(db.Model):
    __tablename__ = 'world_events'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    event_type = db.Column(db.String(80), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    payload = db.Column(db.Text, nullable=True)
    visibility = db.Column(db.String(30), default='dm_private')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self, include_private=False):
        import json

        if self.visibility == 'dm_private' and not include_private:
            return None

        try:
            payload = json.loads(self.payload) if self.payload else {}
        except (TypeError, ValueError):
            payload = {}

        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'event_type': self.event_type,
            'summary': self.summary,
            'payload': payload if include_private else {},
            'visibility': self.visibility,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CampaignMonster(db.Model):
    __tablename__ = 'campaign_monsters'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    monster_id = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    stat_block = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('campaign_id', 'monster_id', name='uq_campaign_monster_campaign_monster'),
    )

    def to_dict(self):
        try:
            stat_block = json.loads(self.stat_block) if self.stat_block else {}
        except (TypeError, ValueError):
            stat_block = {}
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'monster_id': self.monster_id,
            'name': self.name,
            'stat_block': stat_block,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class EncounterMap(db.Model):
    __tablename__ = 'encounter_maps'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    image_filename = db.Column(db.String(260), nullable=False)
    labeled_image_filename = db.Column(db.String(260), nullable=True)
    model = db.Column(db.String(120), nullable=False)
    size = db.Column(db.String(40), nullable=False)
    quality = db.Column(db.String(40), nullable=False)
    grid_json = db.Column(db.Text, nullable=True)
    vtt_setup_json = db.Column(db.Text, nullable=True)
    encounter_state_json = db.Column(db.Text, nullable=True)
    setup_status = db.Column(db.String(20), default='pending')
    setup_error = db.Column(db.String(500), nullable=True)
    created_by_tool = db.Column(db.Boolean, default=True)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    session = db.relationship('CampaignSession')
    placements = db.relationship(
        'EncounterMapPlacement',
        backref='encounter_map',
        lazy=True,
        cascade='all, delete-orphan',
    )

    @staticmethod
    def _json_value(raw_value, default=None):
        if not raw_value:
            return default
        try:
            return json.loads(raw_value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _public_vtt_setup(setup):
        if not isinstance(setup, dict):
            return setup
        return {
            'map_summary': setup.get('map_summary'),
            'friendly_spawn_boxes': setup.get('friendly_spawn_boxes', []),
            'player_start_areas': setup.get('player_start_areas', setup.get('friendly_spawn_boxes', [])),
            'terrain_zones': setup.get('terrain_zones', []),
            'obstacles': setup.get('obstacles', []),
        }

    def to_dict(self, include_private=False):
        vtt_setup = self._json_value(self.vtt_setup_json)
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'session_id': self.session_id,
            'title': self.title,
            'prompt': self.prompt,
            'image_url': f'/api/encounter-maps/{self.id}/image',
            'labeled_image_url': f'/api/encounter-maps/{self.id}/labeled-image' if self.labeled_image_filename else None,
            'model': self.model,
            'size': self.size,
            'quality': self.quality,
            'grid': self._json_value(self.grid_json),
            'vtt_setup': vtt_setup if include_private else self._public_vtt_setup(vtt_setup),
            'encounter_state': self._json_value(self.encounter_state_json),
            'placements': [
                placement.to_dict()
                for placement in sorted(self.placements, key=lambda item: (item.grid_row, item.grid_col, item.id))
            ],
            'setup_status': self.setup_status or 'pending',
            'setup_error': self.setup_error,
            'created_by_tool': self.created_by_tool,
            'is_archived': self.is_archived,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class EncounterMapPlacement(db.Model):
    __tablename__ = 'encounter_map_placements'

    id = db.Column(db.Integer, primary_key=True)
    encounter_map_id = db.Column(db.Integer, db.ForeignKey('encounter_maps.id'), nullable=False, index=True)
    actor_type = db.Column(db.String(20), nullable=False)
    actor_id = db.Column(db.String(100), nullable=False)
    label = db.Column(db.String(200), nullable=False)
    grid_col = db.Column(db.Integer, nullable=False)
    grid_row = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('encounter_map_id', 'actor_type', 'actor_id', name='uq_encounter_map_actor_placement'),
        db.CheckConstraint("actor_type in ('player', 'npc', 'monster')", name='ck_encounter_map_actor_type'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'encounter_map_id': self.encounter_map_id,
            'actor_type': self.actor_type,
            'actor_id': self.actor_id,
            'label': self.label,
            'col': self.grid_col,
            'row': self.grid_row,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignMemoryEmbedding(db.Model):
    __tablename__ = 'campaign_memory_embeddings'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    item_type = db.Column(db.String(40), nullable=False)
    item_id = db.Column(db.String(160), nullable=False)
    visibility = db.Column(db.String(30), default='dm_private')
    canonical_text = db.Column(db.Text, nullable=False)
    text_hash = db.Column(db.String(64), nullable=False)
    embedding_model = db.Column(db.String(120), nullable=False)
    embedding_dimensions = db.Column(db.Integer, nullable=False)
    embedding_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('campaign_id', 'item_type', 'item_id', name='uq_campaign_memory_embedding_item'),
    )

    def to_dict(self, include_vector=False):
        data = {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'item_type': self.item_type,
            'item_id': self.item_id,
            'visibility': self.visibility,
            'canonical_text': self.canonical_text,
            'text_hash': self.text_hash,
            'embedding_model': self.embedding_model,
            'embedding_dimensions': self.embedding_dimensions,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_vector:
            import json
            try:
                data['embedding'] = json.loads(self.embedding_json)
            except (TypeError, ValueError):
                data['embedding'] = []
        return data


class SheetProposal(db.Model):
    __tablename__ = 'sheet_proposals'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=False)
    character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=False)
    dm_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    message_id = db.Column(db.Integer, db.ForeignKey('session_messages.id'), nullable=True)
    reason = db.Column(db.String(500), nullable=False)
    changes = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    applied_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'character_id': self.character_id,
            'dm_user_id': self.dm_user_id,
            'message_id': self.message_id,
            'reason': self.reason,
            'changes': self.changes,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'applied_at': self.applied_at.isoformat() if self.applied_at else None,
        }


class LootBox(db.Model):
    __tablename__ = 'loot_boxes'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    items_json = db.Column(db.Text, nullable=False)
    currency_json = db.Column(db.Text, nullable=False)
    draw_results_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='unopened')
    created_by_session_tool = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    opened_at = db.Column(db.DateTime, nullable=True)
    campaign = db.relationship('Campaign')
    session = db.relationship('CampaignSession')

    def to_dict(self, current_user=None, is_dm=False):
        import json
        items = json.loads(self.items_json) if isinstance(self.items_json, str) else self.items_json or {}
        currency = json.loads(self.currency_json) if isinstance(self.currency_json, str) else self.currency_json or {}
        draws = json.loads(self.draw_results_json) if isinstance(self.draw_results_json, str) else self.draw_results_json or {}

        result = {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'session_id': self.session_id,
            'name': self.name,
            'description': self.description,
            'currency': currency,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
        }

        if self.status == 'unopened':
            result['item_count'] = sum(len(v) for v in items.values()) if isinstance(items, dict) else 0
            result['player_count'] = len(items) if isinstance(items, dict) else 0
            result['pools'] = {} if not is_dm else items
            result['draws'] = None
        else:
            result['pools'] = {} if not is_dm else items
            result['draws'] = draws

        if current_user and hasattr(current_user, 'id'):
            user_char_ids = {
                c.id for c in Character.query.filter_by(user_id=current_user.id).all()
            }
            if draws:
                user_draws = {
                    str(k): v for k, v in draws.items()
                    if int(k) in user_char_ids or is_dm
                }
                result['draws'] = user_draws
            if is_dm:
                result['pools'] = items
            elif isinstance(items, dict):
                result['pools'] = {
                    str(k): v for k, v in items.items()
                    if int(k) in user_char_ids
                }

        return result


class CampaignAuditEvent(db.Model):
    __tablename__ = 'campaign_audit_events'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    source = db.Column(db.String(120), nullable=True)
    actor = db.Column(db.String(120), nullable=True)
    trace_id = db.Column(db.String(160), nullable=True, index=True)
    parent_trace_id = db.Column(db.String(160), nullable=True, index=True)
    trace_label = db.Column(db.String(200), nullable=True)
    audit_role = db.Column(db.String(20), nullable=True)
    summary = db.Column(db.Text, nullable=False)
    payload = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        import json

        try:
            payload = json.loads(self.payload) if self.payload else {}
        except (TypeError, ValueError):
            payload = {'raw': self.payload}

        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'event_type': self.event_type,
            'source': self.source,
            'actor': self.actor,
            'trace_id': self.trace_id,
            'parent_trace_id': self.parent_trace_id,
            'trace_label': self.trace_label,
            'audit_role': self.audit_role,
            'summary': self.summary,
            'payload': payload,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CampaignShop(db.Model):
    __tablename__ = 'campaign_shops'

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    location_id = db.Column(db.String(160), nullable=True)
    location_name = db.Column(db.String(200), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    items_json = db.Column(db.Text, nullable=False)  # JSON array of shop items
    is_open = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign = db.relationship('Campaign')

    def to_dict(self):
        import json
        try:
            items = json.loads(self.items_json) if self.items_json else []
        except (TypeError, ValueError):
            items = []
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'location_id': self.location_id,
            'location_name': self.location_name,
            'name': self.name,
            'description': self.description,
            'items': items,
            'is_open': bool(self.is_open),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CampaignMemoryRun(db.Model):
    __tablename__ = 'campaign_memory_runs'

    id = db.Column(db.Integer, primary_key=True)
    memory_run_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=True, index=True)
    source_player_message_id = db.Column(db.Integer, nullable=True)
    source_dm_message_id = db.Column(db.Integer, nullable=True)
    trace_id = db.Column(db.String(100), nullable=True, index=True)

    prompt_chars = db.Column(db.Integer, nullable=True)
    prompt_tokens_estimate = db.Column(db.Integer, nullable=True)
    response_chars = db.Column(db.Integer, nullable=True)
    context_breakdown_json = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'memory_run_id': self.memory_run_id,
            'campaign_id': self.campaign_id,
            'session_id': self.session_id,
            'source_player_message_id': self.source_player_message_id,
            'source_dm_message_id': self.source_dm_message_id,
            'trace_id': self.trace_id,
            'prompt_chars': self.prompt_chars,
            'prompt_tokens_estimate': self.prompt_tokens_estimate,
            'response_chars': self.response_chars,
            'context_breakdown': self.context_breakdown_json,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CampaignMemoryLog(db.Model):
    __tablename__ = 'campaign_memory_logs'

    id = db.Column(db.Integer, primary_key=True)

    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=True, index=True)

    memory_run_id = db.Column(db.String(100), nullable=False, index=True)
    trace_id = db.Column(db.String(100), nullable=True, index=True)
    turn_id = db.Column(db.String(100), nullable=True, index=True)

    source_player_message_id = db.Column(db.Integer, nullable=True)
    source_dm_message_id = db.Column(db.Integer, nullable=True)

    memory_id = db.Column(db.String(200), nullable=True, index=True)
    target_table = db.Column(db.String(100), nullable=True)
    target_id = db.Column(db.String(200), nullable=True)

    operation = db.Column(db.String(50), nullable=False)  # create | update | retire | no-op
    status = db.Column(db.String(50), nullable=False, default='applied')  # applied | skipped | failed | validation_failed | no_op

    memory_type = db.Column(db.String(50), nullable=True)  # npc | fact | relation | clock | location | quest | inventory | money
    visibility = db.Column(db.String(50), nullable=True)
    certainty = db.Column(db.String(50), nullable=True)
    importance = db.Column(db.Integer, nullable=True)

    reason = db.Column(db.Text, nullable=True)
    expires_or_retire_condition = db.Column(db.Text, nullable=True)

    before_json = db.Column(db.JSON, nullable=True)
    after_json = db.Column(db.JSON, nullable=True)
    patch_json = db.Column(db.JSON, nullable=True)

    error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.Index('ix_campaign_memory_logs_campaign_created', 'campaign_id', 'created_at'),
        db.Index('ix_campaign_memory_logs_run', 'memory_run_id'),
        db.Index('ix_campaign_memory_logs_memory', 'campaign_id', 'memory_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'session_id': self.session_id,
            'memory_run_id': self.memory_run_id,
            'trace_id': self.trace_id,
            'turn_id': self.turn_id,
            'source_player_message_id': self.source_player_message_id,
            'source_dm_message_id': self.source_dm_message_id,
            'memory_id': self.memory_id,
            'target_table': self.target_table,
            'target_id': self.target_id,
            'operation': self.operation,
            'status': self.status,
            'memory_type': self.memory_type,
            'visibility': self.visibility,
            'certainty': self.certainty,
            'importance': self.importance,
            'reason': self.reason,
            'expires_or_retire_condition': self.expires_or_retire_condition,
            'before_json': self.before_json,
            'after_json': self.after_json,
            'patch_json': self.patch_json,
            'error': self.error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
