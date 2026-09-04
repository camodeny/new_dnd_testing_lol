"""Characters and character sheets domain models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    system: Mapped[str] = mapped_column(String(32), nullable=False, default="dnd5e")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        return {"id": str(self.id), "owner_id": str(self.owner_id), "system": self.system, "name": self.name}


class CharacterChatMessage(Base):
    __tablename__ = "character_chat_messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    character_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {"id": str(self.id), "owner_id": str(self.owner_id), "character_id": str(self.character_id) if self.character_id else None, "role": self.role, "content": self.content, "created_at": self.created_at.isoformat() if self.created_at else None}


class Dnd5eCharacterSheet(Base):
    __tablename__ = "dnd5e_character_sheets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    character_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    character_name: Mapped[str] = mapped_column(String(128), nullable=False)
    player_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    race: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subrace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    background: Mapped[str | None] = mapped_column(String(64), nullable=True)
    alignment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    char_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    experience_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    strength: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    dexterity: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    constitution: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    intelligence: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    wisdom: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    charisma: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    inspiration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proficiency_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    str_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dex_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    con_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    int_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wis_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cha_save_prof: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    skill_expertise: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    skills: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    saving_throws: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    passive_perception: Mapped[int | None] = mapped_column(Integer, nullable=True)
    armor_class: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    initiative_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speed: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    speed_details: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hit_points_max: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_current: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    hit_points_temp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_dice: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hit_dice_total: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hit_dice_remaining: Mapped[str | None] = mapped_column(String(32), nullable=True)
    death_save_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    death_save_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exhaustion_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encumbrance_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attacks: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    weapons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    spellcasting_ability: Mapped[str | None] = mapped_column(String(16), nullable=True)
    spell_save_dc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_attack_bonus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spell_slots: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    spells: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cantrips: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    equipment: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    equipment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    treasure: Mapped[str | None] = mapped_column(Text, nullable=True)
    cp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ep: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    encumbrance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    features_and_traits: Mapped[str | None] = mapped_column(Text, nullable=True)
    features: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    proficiencies: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    other_proficiencies_languages: Mapped[str | None] = mapped_column(Text, nullable=True)
    proficiencies_languages_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    companions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    conditions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    extras: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    portrait_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self):
        base = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        base["id"] = str(base["id"]) if base.get("id") else None
        base["character_id"] = str(base["character_id"]) if base.get("character_id") else None
        base["owner_id"] = str(base["owner_id"]) if base.get("owner_id") else None
        base["name"] = base.get("character_name")
        base["total_level"] = base.get("level")
        base["max_hp"] = base.get("hit_points_max")
        base["current_hp"] = base.get("hit_points_current")
        base["temp_hp"] = base.get("hit_points_temp")
        # Canonical scalar for frontend Character.hit_points (number) readers.
        base["hit_points"] = base.get("hit_points_current")
        base["encumbrance_status"] = base.get("encumbrance_status") or base.get("encumbrance")
        # Frontend-canonical flat aliases for columns with different names.
        base["character_appearance"] = base.get("appearance")
        base["allies_organizations"] = base.get("allies_and_organizations")
        base["additional_features_traits"] = base.get("features_and_traits")
        if base.get("weapons") is None and base.get("attacks") is not None:
            base["weapons"] = base["attacks"]
        # Nested CharacterDraft groups (additive). `appearance` stays the flat
        # text column; its group is reconstructible from the flat aliases above.
        base["ability_scores"] = {k: base.get(k) for k in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")}
        base["combat"] = {
            "max_hp": base.get("hit_points_max"),
            "current_hp": base.get("hit_points_current"),
            "temp_hp": base.get("hit_points_temp"),
            "armor_class": base.get("armor_class"),
            "initiative_bonus": base.get("initiative_bonus"),
            "speed": base.get("speed"),
            "death_save_successes": base.get("death_save_successes"),
            "death_save_failures": base.get("death_save_failures"),
        }
        base["general"] = {
            "inspiration": base.get("inspiration"),
            "proficiency_bonus": base.get("proficiency_bonus"),
            "passive_perception": base.get("passive_perception"),
            "exhaustion_level": base.get("exhaustion_level"),
            "encumbrance_status": base.get("encumbrance_status"),
        }
        base["spellcasting"] = {
            "spellcasting_ability": base.get("spellcasting_ability"),
            "spell_save_dc": base.get("spell_save_dc"),
            "spell_attack_bonus": base.get("spell_attack_bonus"),
            "spell_slots": base.get("spell_slots"),
        }
        base["currency"] = {k: base.get(k) for k in ("cp", "sp", "ep", "gp", "pp")}
        base["personality"] = {k: base.get(k) for k in ("personality_traits", "ideals", "bonds", "flaws")}
        base["background_details"] = {
            "backstory": base.get("backstory"),
            "allies_organizations": base.get("allies_and_organizations"),
            "additional_features_traits": base.get("features_and_traits"),
            "treasure": base.get("treasure"),
        }
        return base

    @classmethod
    def from_frontend(cls, data: dict, owner_id: uuid.UUID):
        """Map every editable CharacterDraft field (flat or nested) to columns.

        Accepts both the flat spellings emitted by
        ``toCharacterPayload`` (e.g. ``strength``, ``max_hp``, ``cp``,
        ``personality_traits``) and the nested groups (``ability_scores``,
        ``combat``, ``general``, ``spellcasting``, ``currency``,
        ``personality``, ``appearance``, ``background_details``,
        ``hit_points``). Nested groups win over flat keys, mirroring the
        frontend ``mergeCharacterDraft`` precedence.
        """
        data = data or {}

        def _group(name: str) -> dict:
            value = data.get(name)
            return value if isinstance(value, dict) else {}

        ability = _group("ability_scores")
        combat = _group("combat")
        general = _group("general")
        spellcasting = _group("spellcasting")
        currency = _group("currency")
        personality = _group("personality")
        appearance_group = _group("appearance")
        background_details = _group("background_details")
        hit_points_group = data.get("hit_points")
        if not isinstance(hit_points_group, dict):
            hit_points_group = {}

        def _pick(group: dict, *keys):
            for key in keys:
                if key in group:
                    return group[key]
            for key in keys:
                if key in data:
                    return data[key]
            return None

        def _present(*keys) -> bool:
            return any(k in data for k in keys)

        mapped = {}
        if "name" in data:
            mapped["character_name"] = data["name"]
        elif "character_name" in data:
            mapped["character_name"] = data["character_name"]
        else:
            mapped["character_name"] = "Unnamed Hero"
        for k in ("player_name", "race", "subrace", "background", "alignment", "experience_points"):
            if k in data:
                mapped[k] = data[k]
        if "total_level" in data:
            mapped["level"] = data["total_level"]
        elif "level" in data:
            mapped["level"] = data["level"]
        for ability_key in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"):
            if ability_key in ability or ability_key in data:
                mapped[ability_key] = _pick(ability, ability_key)
        # Hit points: nested combat > hit_points dict/scalar > flat aliases.
        hp_max = _pick(combat, "max_hp", "hit_points_max", "maximum", "max")
        if hp_max is None:
            hp_max = _pick(hit_points_group, "maximum", "max", "max_hp", "hit_points_max")
        if hp_max is None and _present("hit_points_max"):
            hp_max = data["hit_points_max"]
        if hp_max is not None:
            mapped["hit_points_max"] = hp_max
        hp_current = _pick(combat, "current_hp", "hit_points_current", "current")
        if hp_current is None:
            hp_current = _pick(hit_points_group, "current", "current_hp", "hit_points_current")
        if hp_current is None and isinstance(data.get("hit_points"), (int, float)) and not isinstance(data.get("hit_points"), bool):
            hp_current = data["hit_points"]
        if hp_current is None and _present("current_hp", "hit_points_current"):
            hp_current = data.get("current_hp", data.get("hit_points_current"))
        if hp_current is not None:
            mapped["hit_points_current"] = hp_current
        hp_temp = _pick(combat, "temp_hp", "hit_points_temp", "temporary", "temp")
        if hp_temp is None:
            hp_temp = _pick(hit_points_group, "temporary", "temp", "temp_hp", "hit_points_temp")
        if hp_temp is None and _present("temp_hp", "hit_points_temp"):
            hp_temp = data.get("temp_hp", data.get("hit_points_temp"))
        if hp_temp is not None:
            mapped["hit_points_temp"] = hp_temp
        for k in ("armor_class", "initiative_bonus", "speed", "death_save_successes", "death_save_failures"):
            if k in combat or k in data:
                mapped[k] = _pick(combat, k)
        for k in ("inspiration", "proficiency_bonus", "passive_perception", "exhaustion_level"):
            if k in general or k in data:
                mapped[k] = _pick(general, k)
        for k in ("classes", "skills", "saving_throws", "proficiencies", "features", "weapons", "equipment", "spells", "resources", "companions", "conditions", "cantrips"):
            if k in data:
                mapped[k] = data[k]
        if "attacks" in data and "weapons" not in mapped:
            mapped["weapons"] = data["attacks"]
        for k in ("spellcasting_ability", "spell_save_dc", "spell_attack_bonus", "spell_slots"):
            if k in spellcasting or k in data:
                mapped[k] = _pick(spellcasting, k)
        for k in ("cp", "sp", "ep", "gp", "pp"):
            if k in currency or k in data:
                mapped[k] = _pick(currency, k)
        for k in ("personality_traits", "ideals", "bonds", "flaws"):
            if k in personality or k in data:
                mapped[k] = _pick(personality, k)
        for k in ("age", "height", "weight", "eyes", "skin", "hair"):
            if k in appearance_group or k in data:
                mapped[k] = _pick(appearance_group, k)
        appearance_text = _pick(appearance_group, "character_appearance", "appearance")
        if appearance_text is None and isinstance(data.get("appearance"), str):
            appearance_text = data["appearance"]
        if appearance_text is None and isinstance(data.get("character_appearance"), str):
            appearance_text = data["character_appearance"]
        if appearance_text is not None:
            mapped["appearance"] = appearance_text
        backstory = _pick(background_details, "backstory")
        if backstory is not None:
            mapped["backstory"] = backstory
        allies = _pick(background_details, "allies_organizations", "allies_and_organizations")
        if allies is None and _present("allies_and_organizations"):
            allies = data["allies_and_organizations"]
        if allies is not None:
            mapped["allies_and_organizations"] = allies
        traits = _pick(background_details, "additional_features_traits", "features_and_traits")
        if traits is None and _present("features_and_traits"):
            traits = data["features_and_traits"]
        if traits is not None:
            mapped["features_and_traits"] = traits
        treasure = _pick(background_details, "treasure")
        if treasure is not None:
            mapped["treasure"] = treasure
        if "encumbrance_status" in general or "encumbrance_status" in data or "encumbrance" in data:
            enc = _pick(general, "encumbrance_status", "encumbrance")
            if enc is None:
                enc = data.get("encumbrance_status", data.get("encumbrance"))
            mapped["encumbrance_status"] = enc
            mapped["encumbrance"] = enc
        # Passthrough for any other known column sent flat (hit dice,
        # equipment text, portrait, notes, etc.).
        for col in cls.__table__.columns:
            key = col.name
            if key in mapped or key in ("id", "character_id", "owner_id", "created_at", "updated_at", "appearance"):
                continue
            if key in data:
                mapped[key] = data[key]
        mapped["owner_id"] = owner_id
        return cls(**{k: v for k, v in mapped.items() if k in cls.__table__.columns})
