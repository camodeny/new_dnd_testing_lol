import { useRef, useState } from 'react'
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
import {
  CHARACTER_FORM_PAGES,
  ITEM_LIST_CONFIGS,
  flattenCharacter,
  mergeCharacterDraft,
  normalizeItemList,
} from './characterFormConfig'

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
  const [activePageIndex, setActivePageIndex] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const formRef = useRef(null)

  const activePage = CHARACTER_FORM_PAGES[activePageIndex]
  const isFirstPage = activePageIndex === 0
  const isLastPage = activePageIndex === CHARACTER_FORM_PAGES.length - 1

  const getPanel = (page) => formRef.current?.querySelector(`[data-character-form-panel="${page.key}"]`)

  const getFirstInvalidControl = (container) => {
    if (!container) return null
    return [...container.querySelectorAll('input, select, textarea')]
      .find((control) => control.willValidate && !control.validity.valid)
  }

  const focusPageHeading = (page) => {
    window.requestAnimationFrame(() => {
      getPanel(page)?.querySelector('[data-character-form-heading]')?.focus()
    })
  }

  const showPage = (pageIndex, { focusHeading = true } = {}) => {
    const boundedIndex = Math.min(Math.max(pageIndex, 0), CHARACTER_FORM_PAGES.length - 1)
    const nextPage = CHARACTER_FORM_PAGES[boundedIndex]
    setActivePageIndex(boundedIndex)
    if (focusHeading) focusPageHeading(nextPage)
  }

  const validatePage = (page) => {
    const invalidControl = getFirstInvalidControl(getPanel(page))
    if (!invalidControl) return true
    invalidControl.focus()
    invalidControl.reportValidity()
    return false
  }

  const handleNext = () => {
    if (!validatePage(activePage)) return
    showPage(activePageIndex + 1)
  }

  const handleBack = () => {
    showPage(activePageIndex - 1)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()

    if (!isLastPage) {
      handleNext()
      return
    }

    const invalidControl = getFirstInvalidControl(e.currentTarget)
    if (invalidControl) {
      const invalidPanel = invalidControl.closest('[data-character-form-panel]')
      const invalidPageIndex = CHARACTER_FORM_PAGES.findIndex(
        (page) => page.key === invalidPanel?.dataset.characterFormPanel,
      )

      if (invalidPageIndex >= 0) {
        showPage(invalidPageIndex, { focusHeading: false })
      }
      window.requestAnimationFrame(() => {
        invalidControl.focus()
        invalidControl.reportValidity()
      })
      return
    }

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
    <form
      ref={formRef}
      className="character-form character-form-wizard"
      data-character-form-wizard
      data-active-step={activePage.key}
      noValidate
      onSubmit={handleSubmit}
    >
      <ErrorMessage message={error} />

      <nav className="character-form-wizard__step-nav" aria-label="Character form steps">
        <ol className="character-form-wizard__step-list">
          {CHARACTER_FORM_PAGES.map((page, pageIndex) => {
            const isActive = pageIndex === activePageIndex
            const position = isActive ? 'current' : pageIndex < activePageIndex ? 'before' : 'after'
            const panelId = `character-form-panel-${page.key}`

            return (
              <li
                key={page.key}
                className={`character-form-wizard__step-item is-${position}`}
                data-step-key={page.key}
                data-step-index={pageIndex}
                data-step-position={position}
              >
                <button
                  type="button"
                  className={`character-form-wizard__step-button ${isActive ? 'is-active' : ''}`}
                  data-character-form-step={page.key}
                  aria-current={isActive ? 'step' : undefined}
                  aria-controls={panelId}
                  aria-label={`Step ${pageIndex + 1} of ${CHARACTER_FORM_PAGES.length}: ${page.label}`}
                  onClick={() => showPage(pageIndex)}
                >
                  <span className="character-form-wizard__step-number" aria-hidden="true">
                    {pageIndex + 1}
                  </span>
                  <i className={`bi ${page.icon} character-form-wizard__step-icon`} aria-hidden="true" />
                  <span className="character-form-wizard__step-label">{page.label}</span>
                </button>
              </li>
            )
          })}
        </ol>
      </nav>

      <div className="character-form-wizard__panels">
        {CHARACTER_FORM_PAGES.map((page, pageIndex) => {
          const isActive = pageIndex === activePageIndex
          const panelId = `character-form-panel-${page.key}`
          const headingId = `character-form-heading-${page.key}`

          return (
            <section
              key={page.key}
              id={panelId}
              className={`character-form-wizard__panel ${isActive ? 'is-active' : ''}`}
              data-character-form-panel={page.key}
              data-step-index={pageIndex}
              data-active={isActive ? 'true' : 'false'}
              aria-labelledby={headingId}
              hidden={!isActive}
            >
              <header className="character-form-wizard__panel-header">
                <p className="character-form-wizard__eyebrow">
                  Step {pageIndex + 1} of {CHARACTER_FORM_PAGES.length}
                </p>
                <h2
                  id={headingId}
                  className="character-form-wizard__panel-title"
                  data-character-form-heading
                  tabIndex={-1}
                >
                  {page.label}
                </h2>
              </header>

              <CharacterFormBody
                character={character}
                setCharacter={setCharacter}
                sections={page.sections}
              />
            </section>
          )
        })}
      </div>

      <div className="form-actions character-form-wizard__actions" data-character-form-actions>
        <div className="character-form-wizard__actions-start">
          {onCancel && (
            <Button
              type="button"
              variant="secondary"
              className="character-form-wizard__cancel"
              onClick={onCancel}
            >
              Cancel
            </Button>
          )}
        </div>

        <div className="character-form-wizard__actions-end">
          <Button
            type="button"
            variant="secondary"
            className="character-form-wizard__back"
            disabled={isFirstPage}
            onClick={handleBack}
          >
            <i className="bi bi-chevron-left" aria-hidden="true" /> Back
          </Button>

          {isLastPage ? (
            <Button
              type="submit"
              variant="primary"
              className="character-form-wizard__submit"
              disabled={loading}
            >
              {loading ? 'Saving...' : submitLabel}
            </Button>
          ) : (
            <Button
              type="button"
              variant="primary"
              className="character-form-wizard__next"
              onClick={handleNext}
            >
              Next <i className="bi bi-chevron-right" aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>
    </form>
  )
}
