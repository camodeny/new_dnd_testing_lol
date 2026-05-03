from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    campaigns = db.relationship('Campaign', backref='owner', lazy=True)
    characters = db.relationship('Character', backref='player', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'created_at': self.created_at.isoformat()
        }


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
    is_active = db.Column(db.Boolean, default=True)
    messages = db.relationship('SessionMessage', backref='session', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None,
            'recap': self.recap,
            'is_active': self.is_active,
        }


class SessionMessage(db.Model):
    __tablename__ = 'session_messages'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('campaign_sessions.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
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
    user = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else None,
            'role': self.role,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None,
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
