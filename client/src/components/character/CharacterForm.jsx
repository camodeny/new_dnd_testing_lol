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
import { ITEM_LIST_CONFIGS, flattenCharacter, mergeCharacterDraft, normalizeItemList } from './characterFormConfig'

const ITEM_CONFIG_BY_KEY = Object.fromEntries(ITEM_LIST_CONFIGS.map((config) => [config.key, config]))

function changedKeys(previous, next) {
  const keys = new Set([...Object.keys(previous || {}), ...Object.keys(next || {})])
  return [...keys].filter((key) => previous?.[key] !== next?.[key])
}

function GeneralSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>General</h3>
      <div className="form-grid five-col">
        <FormGroup label="Proficiency Bonus" htmlFor="gen-prof">
          <NumberInput id="gen-prof" value={data.proficiency_bonus} onChange={(v) => set('proficiency_bonus', v)} />
        </FormGroup>
        <FormGroup label="Passive Perception" htmlFor="gen-passive">
          <NumberInput id="gen-passive" value={data.passive_perception} onChange={(v) => set('passive_perception', v)} />
        </FormGroup>
        <FormGroup label="Exhaustion Level" htmlFor="gen-exhaust">
          <NumberInput id="gen-exhaust" value={data.exhaustion_level} onChange={(v) => set('exhaustion_level', v)} min={0} max={6} />
        </FormGroup>
        <FormGroup label="Encumbrance" htmlFor="gen-enc">
          <select
            id="gen-enc"
            value={data.encumbrance_status}
            onChange={(e) => set('encumbrance_status', e.target.value)}
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
            checked={data.inspiration}
            onChange={(e) => set('inspiration', e.target.checked)}
          />
          Inspiration
        </label>
      </div>
    </div>
  )
}

export function CharacterFormBody({ character, setCharacter, sections, onFieldTouched }) {
  const markTouched = (paths) => {
    if (!onFieldTouched) return
    paths.forEach((path) => onFieldTouched(path))
  }

  const updateTopLevelFields = (data) => {
    setCharacter((prev) => {
      markTouched(changedKeys(prev, data))
      return { ...prev, ...data }
    })
  }

  const updateField = (field, value) => {
    setCharacter((prev) => {
      markTouched([field])
      return { ...prev, [field]: value }
    })
  }

  const updateGroup = (group, data) => {
    setCharacter((prev) => {
      markTouched(changedKeys(prev[group], data).map((key) => `${group}.${key}`))
      return { ...prev, [group]: data }
    })
  }

  const renderItemList = (key) => {
    const config = ITEM_CONFIG_BY_KEY[key]
    if (!config) return null
    const editorConfig = { ...config }
    delete editorConfig.key
    return (
      <ItemListEditor
        key={key}
        items={normalizeItemList(key, character[key] || [])}
        onChange={(items) => updateField(key, items)}
        {...editorConfig}
      />
    )
  }

  const renderSection = (section) => {
    switch (section) {
      case 'basic':
        return <BasicInfoSection key={section} data={character} onChange={updateTopLevelFields} />
      case 'ability_scores':
        return <AbilityScoresSection key={section} data={character.ability_scores} onChange={(data) => updateGroup('ability_scores', data)} />
      case 'combat':
        return <CombatStatsSection key={section} data={character.combat} onChange={(data) => updateGroup('combat', data)} />
      case 'general':
        return <GeneralSection key={section} data={character.general} onChange={(data) => updateGroup('general', data)} />
      case 'spellcasting':
        return <SpellcastingSection key={section} data={character.spellcasting} onChange={(data) => updateGroup('spellcasting', data)} />
      case 'currency':
        return <CurrencySection key={section} data={character.currency} onChange={(data) => updateGroup('currency', data)} />
      case 'personality':
        return <PersonalitySection key={section} data={character.personality} onChange={(data) => updateGroup('personality', data)} />
      case 'appearance':
        return <AppearanceSection key={section} data={character.appearance} onChange={(data) => updateGroup('appearance', data)} />
      case 'background_details':
        return <BackgroundSection key={section} data={character.background_details} onChange={(data) => updateGroup('background_details', data)} />
      default:
        return renderItemList(section)
    }
  }

  return sections.map(renderSection)
}

export default function CharacterForm({ initialCharacter, onSubmit, onCancel, submitLabel = 'Save Character' }) {
  const [character, setCharacter] = useState(() => mergeCharacterDraft(initialCharacter))
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

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

      <CharacterFormBody
        character={character}
        setCharacter={setCharacter}
        sections={[
          'basic',
          'ability_scores',
          'combat',
          'general',
          'spellcasting',
          'currency',
          'personality',
          'appearance',
          'background_details',
          ...ITEM_LIST_CONFIGS.map(({ key }) => key),
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
