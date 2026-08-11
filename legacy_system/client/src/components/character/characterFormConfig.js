const emptySpellSlots = () =>
  Object.fromEntries(Array.from({ length: 9 }, (_, i) => [i + 1, { max: 0, used: 0 }]))

export function makeEmptyCharacter() {
  return {
    name: '',
    player_name: '',
    race: '',
    subrace: '',
    alignment: '',
    background: '',
    experience_points: 0,
    total_level: 1,
    ability_scores: { strength: 10, dexterity: 10, constitution: 10, intelligence: 10, wisdom: 10, charisma: 10 },
    combat: { max_hp: 1, current_hp: 1, temp_hp: 0, armor_class: 10, initiative_bonus: 0, speed: 30, death_save_successes: 0, death_save_failures: 0 },
    general: { inspiration: false, proficiency_bonus: 2, passive_perception: 10, exhaustion_level: 0, encumbrance_status: 'normal' },
    spellcasting: { spellcasting_ability: '', spell_save_dc: null, spell_attack_bonus: null, spell_slots: emptySpellSlots() },
    currency: { cp: 0, sp: 0, ep: 0, gp: 0, pp: 0 },
    personality: { personality_traits: '', ideals: '', bonds: '', flaws: '' },
    appearance: { age: '', height: '', weight: '', eyes: '', skin: '', hair: '', character_appearance: '' },
    background_details: { backstory: '', allies_organizations: '', additional_features_traits: '', treasure: '' },
    classes: [],
    skills: [],
    saving_throws: [],
    proficiencies: [],
    features: [],
    weapons: [],
    equipment: [],
    spells: [],
    notes: [],
    resources: [],
    companions: [],
    conditions: [],
  }
}

export const CHARACTER_FORM_PAGES = [
  {
    key: 'identity',
    label: 'Identity',
    icon: 'bi-person-vcard',
    sections: ['basic', 'classes'],
  },
  {
    key: 'scores',
    label: 'Scores',
    icon: 'bi-bar-chart',
    sections: ['ability_scores', 'skills', 'saving_throws', 'proficiencies'],
  },
  {
    key: 'combat',
    label: 'Combat',
    icon: 'bi-shield-shaded',
    sections: ['combat', 'general', 'weapons', 'resources'],
  },
  {
    key: 'magic_gear',
    label: 'Magic & Gear',
    icon: 'bi-stars',
    sections: ['spellcasting', 'spells', 'equipment', 'currency'],
  },
  {
    key: 'story',
    label: 'Story',
    icon: 'bi-book',
    sections: ['personality', 'appearance', 'background_details', 'features', 'notes', 'companions', 'conditions'],
  },
]

function mergeGroup(base, value) {
  return { ...(base || {}), ...(value || {}) }
}

const ITEM_LIST_FIELD_ALIASES = {
  classes: {
    class_name: ['name', 'class', 'className'],
    subclass: ['archetype', 'subclassName'],
    hit_die_type: ['hit_die', 'hitDie', 'hitDieType'],
  },
  skills: {
    skill_name: ['name', 'skill', 'skillName'],
  },
  saving_throws: {
    ability: ['name', 'saving_throw', 'savingThrow'],
  },
  proficiencies: {
    name: ['proficiency_name', 'proficiencyName'],
    proficiency_type: ['type', 'proficiencyType'],
  },
  features: {
    name: ['feature_name', 'featureName', 'title'],
  },
  weapons: {
    name: ['weapon_name', 'weaponName', 'title'],
  },
  equipment: {
    name: ['item_name', 'itemName', 'equipment_name', 'equipmentName', 'title'],
    equipment_type: ['type', 'item_type', 'itemType', 'equipmentType'],
  },
  spells: {
    name: ['spell_name', 'spellName', 'title'],
    spell_level: ['level', 'spellLevel'],
  },
  notes: {
    title: ['name', 'note_title', 'noteTitle'],
    content: ['description', 'text', 'notes'],
  },
  resources: {
    name: ['resource_name', 'resourceName', 'title'],
    max: ['maximum', 'max_amount', 'maxAmount'],
  },
  companions: {
    name: ['companion_name', 'companionName', 'title'],
    companion_type: ['type', 'companionType'],
  },
  conditions: {
    condition_name: ['name', 'condition', 'conditionName', 'title'],
  },
}

const ITEM_LIST_STRING_FIELDS = {
  proficiencies: 'name',
}

const ITEM_LIST_STRING_DEFAULTS = {
  skills: { is_proficient: true },
  saving_throws: { is_proficient: true },
}

function hasUsableValue(value) {
  return value !== undefined && value !== null && (typeof value !== 'string' || value.trim() !== '')
}

function firstUsableKey(source, keys) {
  return keys.find((key) => hasUsableValue(source[key]))
}

export function normalizeItemList(key, items) {
  if (!Array.isArray(items)) return items
  const config = ITEM_LIST_CONFIGS.find((itemConfig) => itemConfig.key === key)
  if (!config) return items
  const aliases = ITEM_LIST_FIELD_ALIASES[key] || {}

  return items.map((item) => {
    if (typeof item === 'string') {
      return {
        ...config.emptyItem,
        ...(ITEM_LIST_STRING_DEFAULTS[key] || {}),
        [ITEM_LIST_STRING_FIELDS[key] || config.fields[0].key]: item,
      }
    }
    if (!item || typeof item !== 'object' || Array.isArray(item)) return item
    const normalized = { ...config.emptyItem, ...item }
    config.fields.forEach((field) => {
      const sourceKey = firstUsableKey(item, [field.key, ...(aliases[field.key] || [])])
      if (sourceKey) normalized[field.key] = item[sourceKey]
    })
    return normalized
  })
}

function setExpandedPath(target, path, value) {
  const parts = path.split('.').filter(Boolean)
  if (parts.length < 2) {
    target[path] = value
    return
  }

  let cursor = target
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index]
    if (!cursor[part] || typeof cursor[part] !== 'object' || Array.isArray(cursor[part])) {
      cursor[part] = {}
    }
    cursor = cursor[part]
  }
  cursor[parts[parts.length - 1]] = value
}

function expandDottedKeys(source) {
  const expanded = {}
  Object.entries(source).forEach(([key, value]) => {
    if (key.includes('.')) {
      setExpandedPath(expanded, key, value)
    } else if (
      value
      && typeof value === 'object'
      && !Array.isArray(value)
      && expanded[key]
      && typeof expanded[key] === 'object'
      && !Array.isArray(expanded[key])
    ) {
      expanded[key] = { ...expanded[key], ...value }
    } else {
      expanded[key] = value
    }
  })
  return expanded
}

export function normalizeCharacterDraft(draft) {
  if (!draft || typeof draft !== 'object') return draft
  const normalized = expandDottedKeys(draft)
  ITEM_LIST_CONFIGS.forEach(({ key }) => {
    if (key in normalized) normalized[key] = normalizeItemList(key, normalized[key])
  })
  return normalized
}

export function mergeCharacterDraft(draft) {
  const empty = makeEmptyCharacter()
  const normalizedDraft = normalizeCharacterDraft(draft)
  if (!normalizedDraft) return empty
  return {
    ...empty,
    ...normalizedDraft,
    ability_scores: mergeGroup(empty.ability_scores, normalizedDraft.ability_scores),
    combat: mergeGroup(empty.combat, normalizedDraft.combat),
    general: mergeGroup(empty.general, normalizedDraft.general),
    spellcasting: {
      ...mergeGroup(empty.spellcasting, normalizedDraft.spellcasting),
      spell_slots: mergeGroup(empty.spellcasting.spell_slots, normalizedDraft.spellcasting?.spell_slots),
    },
    currency: mergeGroup(empty.currency, normalizedDraft.currency),
    personality: mergeGroup(empty.personality, normalizedDraft.personality),
    appearance: mergeGroup(empty.appearance, normalizedDraft.appearance),
    background_details: mergeGroup(empty.background_details, normalizedDraft.background_details),
    classes: normalizedDraft.classes || empty.classes,
    skills: normalizedDraft.skills || empty.skills,
    saving_throws: normalizedDraft.saving_throws || empty.saving_throws,
    proficiencies: normalizedDraft.proficiencies || empty.proficiencies,
    features: normalizedDraft.features || empty.features,
    weapons: normalizedDraft.weapons || empty.weapons,
    equipment: normalizedDraft.equipment || empty.equipment,
    spells: normalizedDraft.spells || empty.spells,
    notes: normalizedDraft.notes || empty.notes,
    resources: normalizedDraft.resources || empty.resources,
    companions: normalizedDraft.companions || empty.companions,
    conditions: normalizedDraft.conditions || empty.conditions,
  }
}

export function flattenCharacter(character) {
  const normalizedCharacter = normalizeCharacterDraft(character)
  return {
    ...normalizedCharacter,
    ...normalizedCharacter.ability_scores,
    ...normalizedCharacter.combat,
    ...normalizedCharacter.general,
    ...normalizedCharacter.spellcasting,
    ...normalizedCharacter.currency,
    ...normalizedCharacter.personality,
    ...normalizedCharacter.appearance,
    ...normalizedCharacter.background_details,
  }
}

export const ITEM_LIST_CONFIGS = [
  {
    key: 'classes',
    title: 'Classes',
    emptyItem: { class_name: '', subclass: '', level: 1, hit_die_type: 'd8' },
    fields: [
      { key: 'class_name', label: 'Class Name' },
      { key: 'subclass', label: 'Subclass' },
      { key: 'level', label: 'Level', type: 'number' },
      { key: 'hit_die_type', label: 'Hit Die' },
    ],
  },
  {
    key: 'skills',
    title: 'Skills',
    emptyItem: { skill_name: '', is_proficient: false, is_expertise: false, bonus_override: null },
    fields: [
      { key: 'skill_name', label: 'Skill Name' },
      { key: 'is_proficient', label: 'Proficient', type: 'checkbox' },
      { key: 'is_expertise', label: 'Expertise', type: 'checkbox' },
      { key: 'bonus_override', label: 'Bonus Override', type: 'number' },
    ],
  },
  {
    key: 'saving_throws',
    title: 'Saving Throws',
    emptyItem: { ability: '', is_proficient: false, bonus_override: null },
    fields: [
      { key: 'ability', label: 'Ability' },
      { key: 'is_proficient', label: 'Proficient', type: 'checkbox' },
      { key: 'bonus_override', label: 'Bonus Override', type: 'number' },
    ],
  },
  {
    key: 'proficiencies',
    title: 'Proficiencies',
    emptyItem: { proficiency_type: '', name: '', notes: '' },
    fields: [
      { key: 'proficiency_type', label: 'Type (language/weapon/armor/tool)' },
      { key: 'name', label: 'Name' },
      { key: 'notes', label: 'Notes', type: 'textarea' },
    ],
  },
  {
    key: 'features',
    title: 'Features',
    emptyItem: { name: '', source: '', description: '', uses_max: null, uses_current: null, recharge: '' },
    fields: [
      { key: 'name', label: 'Feature Name' },
      { key: 'source', label: 'Source' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'uses_max', label: 'Max Uses', type: 'number' },
      { key: 'uses_current', label: 'Current Uses', type: 'number' },
      { key: 'recharge', label: 'Recharge' },
    ],
  },
  {
    key: 'weapons',
    title: 'Weapons',
    emptyItem: { name: '', attack_bonus: 0, damage: '', damage_type: '', properties: '', notes: '', is_equipped: false },
    fields: [
      { key: 'name', label: 'Weapon Name' },
      { key: 'attack_bonus', label: 'Attack Bonus', type: 'number' },
      { key: 'damage', label: 'Damage (e.g. 1d8+3)' },
      { key: 'damage_type', label: 'Damage Type' },
      { key: 'properties', label: 'Properties' },
      { key: 'notes', label: 'Notes', type: 'textarea' },
      { key: 'is_equipped', label: 'Equipped', type: 'checkbox' },
    ],
  },
  {
    key: 'equipment',
    title: 'Equipment',
    emptyItem: { name: '', equipment_type: '', description: '', quantity: 1, weight: null, is_equipped: false, armor_bonus: null, properties: '' },
    fields: [
      { key: 'name', label: 'Item Name' },
      { key: 'equipment_type', label: 'Type' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'quantity', label: 'Quantity', type: 'number' },
      { key: 'weight', label: 'Weight', type: 'number' },
      { key: 'is_equipped', label: 'Equipped', type: 'checkbox' },
      { key: 'armor_bonus', label: 'Armor Bonus', type: 'number' },
      { key: 'properties', label: 'Properties' },
    ],
  },
  {
    key: 'spells',
    title: 'Spells',
    emptyItem: { name: '', spell_level: 0, school: '', casting_time: '', range: '', components: '', duration: '', description: '', at_higher_levels: '', is_prepared: false, is_ritual: false, is_concentration: false },
    fields: [
      { key: 'name', label: 'Spell Name' },
      { key: 'spell_level', label: 'Level', type: 'number' },
      { key: 'school', label: 'School' },
      { key: 'casting_time', label: 'Casting Time' },
      { key: 'range', label: 'Range' },
      { key: 'components', label: 'Components' },
      { key: 'duration', label: 'Duration' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'at_higher_levels', label: 'At Higher Levels', type: 'textarea' },
      { key: 'is_prepared', label: 'Prepared', type: 'checkbox' },
      { key: 'is_ritual', label: 'Ritual', type: 'checkbox' },
      { key: 'is_concentration', label: 'Concentration', type: 'checkbox' },
    ],
  },
  {
    key: 'resources',
    title: 'Resources',
    emptyItem: { name: '', current: 0, max: 0, recharge: '' },
    fields: [
      { key: 'name', label: 'Resource Name' },
      { key: 'current', label: 'Current', type: 'number' },
      { key: 'max', label: 'Max', type: 'number' },
      { key: 'recharge', label: 'Recharge (short rest / long rest / dawn)' },
    ],
  },
  {
    key: 'companions',
    title: 'Companions',
    emptyItem: { name: '', companion_type: '', max_hp: 1, current_hp: 1, armor_class: null, speed: '', description: '', notes: '' },
    fields: [
      { key: 'name', label: 'Name' },
      { key: 'companion_type', label: 'Type' },
      { key: 'max_hp', label: 'Max HP', type: 'number' },
      { key: 'current_hp', label: 'Current HP', type: 'number' },
      { key: 'armor_class', label: 'AC', type: 'number' },
      { key: 'speed', label: 'Speed' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'notes', label: 'Notes', type: 'textarea' },
    ],
  },
  {
    key: 'conditions',
    title: 'Conditions',
    emptyItem: { condition_name: '', description: '', source: '', is_permanent: false, duration_remaining: '' },
    fields: [
      { key: 'condition_name', label: 'Condition' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'source', label: 'Source' },
      { key: 'is_permanent', label: 'Permanent', type: 'checkbox' },
      { key: 'duration_remaining', label: 'Duration Remaining' },
    ],
  },
]
