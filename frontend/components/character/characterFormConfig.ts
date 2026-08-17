export type FormValue = string | number | boolean | null
export type ItemRecord = Record<string, FormValue>

export interface SpellSlot {
  max: number
  used: number
}

export interface CharacterDraft extends Record<string, unknown> {
  name: string
  player_name: string
  race: string
  subrace: string
  alignment: string
  background: string
  experience_points: number
  total_level: number
  ability_scores: Record<string, number>
  combat: Record<string, number>
  general: Record<string, FormValue>
  spellcasting: {
    spellcasting_ability: string
    spell_save_dc: number | null
    spell_attack_bonus: number | null
    spell_slots: Record<string, SpellSlot>
  }
  currency: Record<string, number>
  personality: Record<string, string>
  appearance: Record<string, string>
  background_details: Record<string, string>
  classes: ItemRecord[]
  skills: ItemRecord[]
  saving_throws: ItemRecord[]
  proficiencies: ItemRecord[]
  features: ItemRecord[]
  weapons: ItemRecord[]
  equipment: ItemRecord[]
  spells: ItemRecord[]
  resources: ItemRecord[]
  companions: ItemRecord[]
  conditions: ItemRecord[]
}

export type CharacterListKey =
  | 'classes'
  | 'skills'
  | 'saving_throws'
  | 'proficiencies'
  | 'features'
  | 'weapons'
  | 'equipment'
  | 'spells'
  | 'resources'
  | 'companions'
  | 'conditions'

export interface ItemFieldConfig {
  key: string
  label: string
  type?: 'text' | 'number' | 'checkbox' | 'textarea'
}

export interface ItemListConfig {
  key: CharacterListKey
  title: string
  emptyItem: ItemRecord
  fields: ItemFieldConfig[]
}

export const CHARACTER_FORM_PAGES = [
  { key: 'identity', label: 'Identity', icon: 'bi-person-vcard', sections: ['basic', 'classes'] },
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
    sections: ['personality', 'appearance', 'background_details', 'features', 'companions', 'conditions'],
  },
] as const

export type CharacterSectionKey = (typeof CHARACTER_FORM_PAGES)[number]['sections'][number]

function emptySpellSlots(): Record<string, SpellSlot> {
  return Object.fromEntries(
    Array.from({ length: 9 }, (_, index) => [String(index + 1), { max: 0, used: 0 }]),
  )
}

export function makeEmptyCharacter(): CharacterDraft {
  return {
    name: '',
    player_name: '',
    race: '',
    subrace: '',
    alignment: '',
    background: '',
    experience_points: 0,
    total_level: 1,
    ability_scores: {
      strength: 10,
      dexterity: 10,
      constitution: 10,
      intelligence: 10,
      wisdom: 10,
      charisma: 10,
    },
    combat: {
      max_hp: 1,
      current_hp: 1,
      temp_hp: 0,
      armor_class: 10,
      initiative_bonus: 0,
      speed: 30,
      death_save_successes: 0,
      death_save_failures: 0,
    },
    general: {
      inspiration: false,
      proficiency_bonus: 2,
      passive_perception: 10,
      exhaustion_level: 0,
      encumbrance_status: 'normal',
    },
    spellcasting: {
      spellcasting_ability: '',
      spell_save_dc: null,
      spell_attack_bonus: null,
      spell_slots: emptySpellSlots(),
    },
    currency: { cp: 0, sp: 0, ep: 0, gp: 0, pp: 0 },
    personality: { personality_traits: '', ideals: '', bonds: '', flaws: '' },
    appearance: {
      age: '',
      height: '',
      weight: '',
      eyes: '',
      skin: '',
      hair: '',
      character_appearance: '',
    },
    background_details: {
      backstory: '',
      allies_organizations: '',
      additional_features_traits: '',
      treasure: '',
    },
    classes: [],
    skills: [],
    saving_throws: [],
    proficiencies: [],
    features: [],
    weapons: [],
    equipment: [],
    spells: [],
    resources: [],
    companions: [],
    conditions: [],
  }
}

export const ITEM_LIST_CONFIGS: ItemListConfig[] = [
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
    emptyItem: {
      name: '', attack_bonus: 0, damage: '', damage_type: '', properties: '', notes: '', is_equipped: false,
    },
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
    emptyItem: {
      name: '', equipment_type: '', description: '', quantity: 1, weight: null, is_equipped: false,
      armor_bonus: null, properties: '',
    },
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
    emptyItem: {
      name: '', spell_level: 0, school: '', casting_time: '', range: '', components: '', duration: '',
      description: '', at_higher_levels: '', is_prepared: false, is_ritual: false, is_concentration: false,
    },
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
    emptyItem: {
      name: '', companion_type: '', max_hp: 1, current_hp: 1, armor_class: null, speed: '',
      description: '', notes: '',
    },
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

const ITEM_CONFIG_BY_KEY = Object.fromEntries(
  ITEM_LIST_CONFIGS.map((config) => [config.key, config]),
) as Record<CharacterListKey, ItemListConfig>

const FIELD_ALIASES: Partial<Record<CharacterListKey, Record<string, string[]>>> = {
  classes: { class_name: ['name', 'class', 'className'], subclass: ['archetype'], hit_die_type: ['hit_die', 'hitDie'] },
  skills: { skill_name: ['name', 'skill', 'skillName'] },
  saving_throws: { ability: ['name', 'saving_throw', 'savingThrow'] },
  proficiencies: { name: ['proficiency_name'], proficiency_type: ['type'] },
  features: { name: ['feature_name', 'title'] },
  weapons: { name: ['weapon_name', 'title'] },
  equipment: { name: ['item_name', 'equipment_name', 'title'], equipment_type: ['type', 'item_type'] },
  spells: { name: ['spell_name', 'title'], spell_level: ['level'] },
  resources: { name: ['resource_name', 'title'], max: ['maximum', 'max_amount'] },
  companions: { name: ['companion_name', 'title'], companion_type: ['type'] },
  conditions: { condition_name: ['name', 'condition', 'title'] },
}

function hasValue(value: unknown): boolean {
  return value !== undefined && value !== null && (typeof value !== 'string' || value.trim() !== '')
}

export function normalizeItemList(key: CharacterListKey, value: unknown): ItemRecord[] {
  if (!Array.isArray(value)) return []
  const config = ITEM_CONFIG_BY_KEY[key]
  const aliases = FIELD_ALIASES[key] ?? {}
  return value.flatMap((item): ItemRecord[] => {
    if (typeof item === 'string') {
      const firstField = key === 'proficiencies' ? 'name' : config.fields[0].key
      return [{ ...config.emptyItem, [firstField]: item }]
    }
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const source = item as Record<string, unknown>
    const normalized = { ...config.emptyItem }
    for (const field of config.fields) {
      const sourceKey = [field.key, ...(aliases[field.key] ?? [])].find((candidate) => hasValue(source[candidate]))
      if (sourceKey) normalized[field.key] = source[sourceKey] as FormValue
    }
    return [normalized]
  })
}

function setPath(target: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split('.')
  let cursor = target
  for (const part of parts.slice(0, -1)) {
    const current = cursor[part]
    if (!current || typeof current !== 'object' || Array.isArray(current)) cursor[part] = {}
    cursor = cursor[part] as Record<string, unknown>
  }
  cursor[parts.at(-1)!] = value
}

export function normalizeCharacterDraft(value?: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const expanded: Record<string, unknown> = {}
  for (const [key, fieldValue] of Object.entries(value)) {
    if (key.includes('.')) setPath(expanded, key, fieldValue)
    else expanded[key] = fieldValue
  }
  for (const config of ITEM_LIST_CONFIGS) {
    if (config.key in expanded) expanded[config.key] = normalizeItemList(config.key, expanded[config.key])
  }
  return expanded
}

function group(
  defaults: Record<string, unknown>,
  nested: unknown,
  flat: Record<string, unknown>,
): Record<string, unknown> {
  const nestedGroup = nested && typeof nested === 'object' && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : {}
  const flatValues = Object.fromEntries(
    Object.keys(defaults).filter((key) => flat[key] !== undefined).map((key) => [key, flat[key]]),
  )
  return { ...defaults, ...flatValues, ...nestedGroup }
}

export function mergeCharacterDraft(value?: unknown): CharacterDraft {
  const empty = makeEmptyCharacter()
  const normalized = normalizeCharacterDraft(value)
  const spellcasting = group(empty.spellcasting, normalized.spellcasting, normalized)
  const suppliedSlots = spellcasting.spell_slots
  spellcasting.spell_slots = {
    ...empty.spellcasting.spell_slots,
    ...(suppliedSlots && typeof suppliedSlots === 'object' ? suppliedSlots : {}),
  }
  const draft = {
    ...empty,
    ...normalized,
    ability_scores: group(empty.ability_scores, normalized.ability_scores, normalized),
    combat: group(empty.combat, normalized.combat, normalized),
    general: group(empty.general, normalized.general, normalized),
    spellcasting,
    currency: group(empty.currency, normalized.currency, normalized),
    personality: group(empty.personality, normalized.personality, normalized),
    appearance: group(empty.appearance, normalized.appearance, normalized),
    background_details: group(empty.background_details, normalized.background_details, normalized),
  } as CharacterDraft
  for (const config of ITEM_LIST_CONFIGS) {
    draft[config.key] = normalizeItemList(config.key, normalized[config.key])
  }
  return draft
}

export function toCharacterPayload(character: CharacterDraft): Record<string, unknown> {
  const normalized = mergeCharacterDraft(character)
  return {
    ...normalized,
    ...normalized.ability_scores,
    ...normalized.combat,
    ...normalized.general,
    ...normalized.spellcasting,
    ...normalized.currency,
    ...normalized.personality,
    ...normalized.appearance,
    ...normalized.background_details,
  }
}
