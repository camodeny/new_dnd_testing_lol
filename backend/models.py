"""App tables that live in Supabase Postgres.

- Supabase Auth owns `auth.users` (managed by GoTrue). Do NOT duplicate it.
- We keep a thin `public.profiles` mirror synced on login (id = auth.users.id UUID).
  This is where app-specific fields live and where foreign keys should point.

If you prefer to use `auth.users` directly without a mirror, point FKs there — but
`profiles` is the Supabase-recommended pattern (lets you add username, etc.).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Profile(Base):
    __tablename__ = "profiles"

    # Matches auth.users.id (UUID v4 from Supabase)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # Supabase auth already guarantees email uniqueness; cache here for queries
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # App display name — set from user_metadata or onboarding
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "username": self.username or (self.email.split("@")[0] if self.email else "adventurer"),
            "email": self.email,
        }


class Dnd5eCharacterSheet(Base):
    """Full D&D 5e character sheet. Named explicitly for 5e to allow future systems.

    Covers the official 5e sheet sections:
    - Identity (name, race, class/level, background, alignment, XP)
    - Ability scores + modifiers (stored as base scores, mods computed)
    - Inspiration / proficiency bonus
    - Saving throws & skills (proficiency flags)
    - Combat (AC, initiative, speed, HP, hit dice, death saves)
    - Attacks, equipment, currency
    - Spellcasting, features & traits, proficiencies/languages
    - Flavor (personality, ideals, bonds, flaws, appearance, backstory)
    - Extras JSONB for homebrew / future fields
    """

    __tablename__ = "dnd5e_character_sheets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # ── Identity ───────────────────────────────────────────────────────
    character_name: Mapped[str] = mapped_column(String(128), nullable=False)
    player_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    race: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subrace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    background: Mapped[str | None] = mapped_column(String(64), nullable=True)
    alignment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Multiclass support via JSONB + denormalized display fields
    char_class: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "Fighter 3 / Wizard 2"
    classes: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"class_name": "Fighter", "level": 3, "subclass": "Champion"}]
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    experience_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Flavor / descriptive
    age: Mapped[str | None] = mapped_column(String(32), nullable=True)
    height: Mapped[str | None] = mapped_column(String(32), nullable=True)
    weight: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eyes: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hair: Mapped[str | None] = mapped_column(String(32), nullable=True)
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    backstory: Mapped[str | None] = mapped_column(Text, nullable=True)
    allies_and_organizations: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality_traits: Mapped[str | None] = mapped_column(Text, nullable=True)
    ideals: Mapped[str | None] = mapped_column(Text, nullable=True)
    bonds: Mapped[str | None] = mapped_column(Text, nullable=True)
    flaws: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Ability Scores (3-30) ──────────────────────────────────────────
    strength: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    dexterity: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    constitution: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    intelligence: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    wisdom: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    charisma: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    # ── Inspiration & Proficiency ──────────────────────────────────────
    inspiration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proficiency_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    # Saving throw proficiencies
    str_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dex_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    con_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    int_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wis_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cha_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Skill proficiencies (18) + expertise flags stored in JSONB for flexibility
    acrobatics_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    animal_handling_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    arcana_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    athletics_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deception_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    insight_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    intimidation_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    investigation_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    medicine_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    nature_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    perception_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    performance_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    persuasion_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    religion_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sleight_of_hand_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stealth_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    survival_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # expertise / custom skill mods
    skill_expertise: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # e.g. {"stealth": true, "perception": true}

    passive_perception: Mapped[int | None] = mapped_column(Integer, nullable=True)  # override, else 10+WIS+prof

    # ── Combat ─────────────────────────────────────────────────────────
    armor_class: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    initiative_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speed: Mapped[int] = mapped_column(Integer, nullable=False, default=30)  # feet per round
    speed_details: Mapped[str | None] = mapped_column(String(128), nullable=True)  # e.g. "fly 30, swim 20"

    hit_points_max: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_current: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_temp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_dice: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "1d8"
    hit_dice_total: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "3d8"
    hit_dice_remaining: Mapped[str | None] = mapped_column(String(32), nullable=True)
    death_save_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    death_save_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Attacks & Spellcasting ─────────────────────────────────────────
    attacks: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Longsword","bonus":5,"damage":"1d8+3","type":"slashing"}]
    spellcasting_ability: Mapped[str | None] = mapped_column(String(16), nullable=True)  # int/wis/cha
    spell_save_dc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_attack_bonus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_slots: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"1": {"total":4,"remaining":3}, ...}
    spells: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Fireball","level":3,"prepared":true}]
    cantrips: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # ── Equipment / Treasure ───────────────────────────────────────────
    equipment: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{"name":"Rope","qty":1,"weight":5}]
    equipment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    treasure: Mapped[str | None] = mapped_column(Text, nullable=True)
    cp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ep: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encumbrance: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── Features, Traits, Proficiencies ────────────────────────────────
    features_and_traits: Mapped[str | None] = mapped_column(Text, nullable=True)  # class/racial features
    other_proficiencies_languages: Mapped[str | None] = mapped_column(Text, nullable=True)
    proficiencies_languages_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # ── Extras ─────────────────────────────────────────────────────────
    extras: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # homebrew / future game fields
    portrait_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
