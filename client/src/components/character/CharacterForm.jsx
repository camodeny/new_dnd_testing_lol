import { useState } from 'react'
import Button from '../common/Button'
import ErrorMessage from '../common/ErrorMessage'
import BasicInfoSection from './sections/BasicInfoSection'
import AbilityScoresSection from './sections/AbilityScoresSection'
import CombatStatsSection from './sections/CombatStatsSection'
import SpellcastingSection from './sections/SpellcastingSection'
import CurrencySection from './sections/CurrencySection'
import PersonalitySection from './sections/PersonalitySection'
import AppearanceSection from './sections/AppearanceSection'
import BackgroundSection from './sections/BackgroundSection'
import ItemListEditor from './sections/ItemListEditor'
import FormGroup from '../common/FormGroup'
import NumberInput from '../common/NumberInput'

function makeEmptyCharacter() {
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
    spellcasting: { spellcasting_ability: '', spell_save_dc: null, spell_attack_bonus: null, spell_slots: Object.fromEntries(Array.from({ length: 9 }, (_, i) => [i + 1, { max: 0, used: 0 }])) },
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

function flattenCharacter(character) {
  return {
    ...character,
    ...character.ability_scores,
    ...character.combat,
    ...character.general,
    ...character.spellcasting,
    ...character.currency,
    ...character.personality,
    ...character.appearance,
    ...character.background_details,
  }
}

export default function CharacterForm({ initialCharacter, onSubmit, onCancel, submitLabel = 'Save Character' }) {
  const [character, setCharacter] = useState(initialCharacter || makeEmptyCharacter())
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const updateField = (field, value) => setCharacter((prev) => ({ ...prev, [field]: value }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const payload = flattenCharacter(character)
      await onSubmit(payload)
    } catch (err) {
      setError(err.message || 'Something went wrong')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form className="character-form" onSubmit={handleSubmit}>
      <ErrorMessage message={error} />

      <BasicInfoSection data={character} onChange={(data) => setCharacter((prev) => ({ ...prev, ...data }))} />

      <AbilityScoresSection data={character.ability_scores} onChange={(data) => updateField('ability_scores', data)} />

      <CombatStatsSection data={character.combat} onChange={(data) => updateField('combat', data)} />

      <div className="form-section">
        <h3>General</h3>
        <div className="form-grid five-col">
          <FormGroup label="Proficiency Bonus" htmlFor="gen-prof">
            <NumberInput id="gen-prof" value={character.general.proficiency_bonus} onChange={(v) => updateField('general', { ...character.general, proficiency_bonus: v })} />
          </FormGroup>
          <FormGroup label="Passive Perception" htmlFor="gen-passive">
            <NumberInput id="gen-passive" value={character.general.passive_perception} onChange={(v) => updateField('general', { ...character.general, passive_perception: v })} />
          </FormGroup>
          <FormGroup label="Exhaustion Level" htmlFor="gen-exhaust">
            <NumberInput id="gen-exhaust" value={character.general.exhaustion_level} onChange={(v) => updateField('general', { ...character.general, exhaustion_level: v })} min={0} max={6} />
          </FormGroup>
          <FormGroup label="Encumbrance" htmlFor="gen-enc">
            <select
              id="gen-enc"
              value={character.general.encumbrance_status}
              onChange={(e) => updateField('general', { ...character.general, encumbrance_status: e.target.value })}
              className="input"
            >
              <option value="normal">Normal</option>
              <option value="encumbered">Encumbered</option>
              <option value="heavily_encumbered">Heavily Encumbered</option>
            </select>
          </FormGroup>
          <label className="checkbox-label inline">
            <input
              type="checkbox"
              checked={character.general.inspiration}
              onChange={(e) => updateField('general', { ...character.general, inspiration: e.target.checked })}
            />
            Inspiration
          </label>
        </div>
      </div>

      <SpellcastingSection data={character.spellcasting} onChange={(data) => updateField('spellcasting', data)} />

      <CurrencySection data={character.currency} onChange={(data) => updateField('currency', data)} />

      <PersonalitySection data={character.personality} onChange={(data) => updateField('personality', data)} />

      <AppearanceSection data={character.appearance} onChange={(data) => updateField('appearance', data)} />

      <BackgroundSection data={character.background_details} onChange={(data) => updateField('background_details', data)} />

      <ItemListEditor
        title="Classes"
        items={character.classes}
        onChange={(items) => updateField('classes', items)}
        emptyItem={{ class_name: '', subclass: '', level: 1, hit_die_type: 'd8' }}
        fields={[
          { key: 'class_name', label: 'Class Name' },
          { key: 'subclass', label: 'Subclass' },
          { key: 'level', label: 'Level', type: 'number' },
          { key: 'hit_die_type', label: 'Hit Die' },
        ]}
      />

      <ItemListEditor
        title="Skills"
        items={character.skills}
        onChange={(items) => updateField('skills', items)}
        emptyItem={{ skill_name: '', is_proficient: false, is_expertise: false, bonus_override: null }}
        fields={[
          { key: 'skill_name', label: 'Skill Name' },
          { key: 'is_proficient', label: 'Proficient', type: 'checkbox' },
          { key: 'is_expertise', label: 'Expertise', type: 'checkbox' },
          { key: 'bonus_override', label: 'Bonus Override', type: 'number' },
        ]}
      />

      <ItemListEditor
        title="Saving Throws"
        items={character.saving_throws}
        onChange={(items) => updateField('saving_throws', items)}
        emptyItem={{ ability: '', is_proficient: false, bonus_override: null }}
        fields={[
          { key: 'ability', label: 'Ability' },
          { key: 'is_proficient', label: 'Proficient', type: 'checkbox' },
          { key: 'bonus_override', label: 'Bonus Override', type: 'number' },
        ]}
      />

      <ItemListEditor
        title="Proficiencies"
        items={character.proficiencies}
        onChange={(items) => updateField('proficiencies', items)}
        emptyItem={{ proficiency_type: '', name: '', notes: '' }}
        fields={[
          { key: 'proficiency_type', label: 'Type (language/weapon/armor/tool)' },
          { key: 'name', label: 'Name' },
          { key: 'notes', label: 'Notes', type: 'textarea' },
        ]}
      />

      <ItemListEditor
        title="Features"
        items={character.features}
        onChange={(items) => updateField('features', items)}
        emptyItem={{ name: '', source: '', description: '', uses_max: null, uses_current: null, recharge: '' }}
        fields={[
          { key: 'name', label: 'Feature Name' },
          { key: 'source', label: 'Source' },
          { key: 'description', label: 'Description', type: 'textarea' },
          { key: 'uses_max', label: 'Max Uses', type: 'number' },
          { key: 'uses_current', label: 'Current Uses', type: 'number' },
          { key: 'recharge', label: 'Recharge' },
        ]}
      />

      <ItemListEditor
        title="Weapons"
        items={character.weapons}
        onChange={(items) => updateField('weapons', items)}
        emptyItem={{ name: '', attack_bonus: 0, damage: '', damage_type: '', properties: '', notes: '', is_equipped: false }}
        fields={[
          { key: 'name', label: 'Weapon Name' },
          { key: 'attack_bonus', label: 'Attack Bonus', type: 'number' },
          { key: 'damage', label: 'Damage (e.g. 1d8+3)' },
          { key: 'damage_type', label: 'Damage Type' },
          { key: 'properties', label: 'Properties' },
          { key: 'notes', label: 'Notes', type: 'textarea' },
          { key: 'is_equipped', label: 'Equipped', type: 'checkbox' },
        ]}
      />

      <ItemListEditor
        title="Equipment"
        items={character.equipment}
        onChange={(items) => updateField('equipment', items)}
        emptyItem={{ name: '', equipment_type: '', description: '', quantity: 1, weight: null, is_equipped: false, armor_bonus: null, properties: '' }}
        fields={[
          { key: 'name', label: 'Item Name' },
          { key: 'equipment_type', label: 'Type' },
          { key: 'description', label: 'Description', type: 'textarea' },
          { key: 'quantity', label: 'Quantity', type: 'number' },
          { key: 'weight', label: 'Weight', type: 'number' },
          { key: 'is_equipped', label: 'Equipped', type: 'checkbox' },
          { key: 'armor_bonus', label: 'Armor Bonus', type: 'number' },
          { key: 'properties', label: 'Properties' },
        ]}
      />

      <ItemListEditor
        title="Spells"
        items={character.spells}
        onChange={(items) => updateField('spells', items)}
        emptyItem={{ name: '', spell_level: 0, school: '', casting_time: '', range: '', components: '', duration: '', description: '', at_higher_levels: '', is_prepared: false, is_ritual: false, is_concentration: false }}
        fields={[
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
        ]}
      />

      <ItemListEditor
        title="Resources"
        items={character.resources}
        onChange={(items) => updateField('resources', items)}
        emptyItem={{ name: '', current: 0, max: 0, recharge: '' }}
        fields={[
          { key: 'name', label: 'Resource Name' },
          { key: 'current', label: 'Current', type: 'number' },
          { key: 'max', label: 'Max', type: 'number' },
          { key: 'recharge', label: 'Recharge (short rest / long rest / dawn)' },
        ]}
      />

      <ItemListEditor
        title="Companions"
        items={character.companions}
        onChange={(items) => updateField('companions', items)}
        emptyItem={{ name: '', companion_type: '', max_hp: 1, current_hp: 1, armor_class: null, speed: '', description: '', notes: '' }}
        fields={[
          { key: 'name', label: 'Name' },
          { key: 'companion_type', label: 'Type' },
          { key: 'max_hp', label: 'Max HP', type: 'number' },
          { key: 'current_hp', label: 'Current HP', type: 'number' },
          { key: 'armor_class', label: 'AC', type: 'number' },
          { key: 'speed', label: 'Speed' },
          { key: 'description', label: 'Description', type: 'textarea' },
          { key: 'notes', label: 'Notes', type: 'textarea' },
        ]}
      />

      <ItemListEditor
        title="Conditions"
        items={character.conditions}
        onChange={(items) => updateField('conditions', items)}
        emptyItem={{ condition_name: '', description: '', source: '', is_permanent: false, duration_remaining: '' }}
        fields={[
          { key: 'condition_name', label: 'Condition' },
          { key: 'description', label: 'Description', type: 'textarea' },
          { key: 'source', label: 'Source' },
          { key: 'is_permanent', label: 'Permanent', type: 'checkbox' },
          { key: 'duration_remaining', label: 'Duration Remaining' },
        ]}
      />

      <div className="form-actions">
        <Button type="submit" variant="primary" disabled={loading}>
          {loading ? 'Saving...' : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  )
}
