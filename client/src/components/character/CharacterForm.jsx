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
import { ITEM_LIST_CONFIGS, flattenCharacter, makeEmptyCharacter } from './characterFormConfig'

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

      {ITEM_LIST_CONFIGS.map(({ key, ...config }) => (
        <ItemListEditor
          key={key}
          items={character[key]}
          onChange={(items) => updateField(key, items)}
          {...config}
        />
      ))}

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
