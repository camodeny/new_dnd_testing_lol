'use client'

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { characters as charactersApi } from '@/lib/api'
import type { Character } from '@/types'
import Button from '@/components/common/Button'
import ErrorMessage from '@/components/common/ErrorMessage'
import FormGroup from '@/components/common/FormGroup'
import NumberInput from '@/components/common/NumberInput'
import AbilityScoresSection from './sections/AbilityScoresSection'
import AppearanceSection from './sections/AppearanceSection'
import BackgroundSection from './sections/BackgroundSection'
import BasicInfoSection from './sections/BasicInfoSection'
import CombatStatsSection from './sections/CombatStatsSection'
import CurrencySection from './sections/CurrencySection'
import ItemListEditor from './sections/ItemListEditor'
import PersonalitySection from './sections/PersonalitySection'
import SpellcastingSection from './sections/SpellcastingSection'
import {
  CHARACTER_FORM_PAGES,
  ITEM_LIST_CONFIGS,
  mergeCharacterDraft,
  normalizeItemList,
  toCharacterPayload,
  type CharacterDraft,
  type CharacterListKey,
  type CharacterSectionKey,
  type FormValue,
} from './characterFormConfig'

interface CharacterFormPageProps {
  initial?: Partial<Character>
  aiPatch?: Partial<CharacterDraft> | null
  onAiPatchApplied?: () => void
  onSaved: (character: Character) => void
  onCancel: () => void
  onToggleAI?: () => void
  aiCollapsed?: boolean
}

const ITEM_CONFIG_BY_KEY = Object.fromEntries(
  ITEM_LIST_CONFIGS.map((config) => [config.key, config]),
)

function GeneralSection({
  data,
  onChange,
}: {
  data: Record<string, FormValue>
  onChange: (data: Record<string, FormValue>) => void
}) {
  const set = (field: string, value: FormValue) => onChange({ ...data, [field]: value })
  return (
    <div className="form-section">
      <h3>General</h3>
      <div className="form-grid five-col">
        <FormGroup label="Proficiency Bonus" htmlFor="gen-proficiency">
          <NumberInput id="gen-proficiency" value={data.proficiency_bonus as number} onChange={(value) => set('proficiency_bonus', value ?? 0)} />
        </FormGroup>
        <FormGroup label="Passive Perception" htmlFor="gen-perception">
          <NumberInput id="gen-perception" value={data.passive_perception as number} onChange={(value) => set('passive_perception', value ?? 0)} />
        </FormGroup>
        <FormGroup label="Exhaustion Level" htmlFor="gen-exhaustion">
          <NumberInput id="gen-exhaustion" value={data.exhaustion_level as number} onChange={(value) => set('exhaustion_level', value ?? 0)} min={0} max={6} />
        </FormGroup>
        <FormGroup label="Encumbrance" htmlFor="gen-encumbrance">
          <select
            id="gen-encumbrance"
            className="input"
            value={String(data.encumbrance_status)}
            onChange={(event) => set('encumbrance_status', event.target.value)}
          >
            <option value="normal">Normal</option>
            <option value="encumbered">Encumbered</option>
            <option value="heavily_encumbered">Heavily Encumbered</option>
          </select>
        </FormGroup>
        <label className="checkbox-label inline">
          <input
            type="checkbox"
            checked={Boolean(data.inspiration)}
            onChange={(event) => set('inspiration', event.target.checked)}
          />
          Inspiration
        </label>
      </div>
    </div>
  )
}

export default function CharacterFormPage({ initial, aiPatch, onAiPatchApplied, onSaved, onCancel, onToggleAI, aiCollapsed }: CharacterFormPageProps) {
  const [draft, setDraft] = useState<CharacterDraft>(() => mergeCharacterDraft(initial))
  const aiPatchRef = useRef(0)

  useEffect(() => {
    if (!aiPatch) return
    // merge AI patch into current draft (preserves manual edits for fields AI didn't touch)
    aiPatchRef.current += 1
    const patchId = aiPatchRef.current
    setDraft((prev) => {
      // deep merge: only overwrite fields that AI actually provided
      const merged = mergeCharacterDraft({ ...prev, ...aiPatch })
      // for nested groups, prefer AI values where defined
      // mergeCharacterDraft already handles nested groups correctly
      return merged
    })
    onAiPatchApplied?.()
  }, [aiPatch, onAiPatchApplied])
  const [activePageIndex, setActivePageIndex] = useState(0)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const formRef = useRef<HTMLFormElement>(null)
  const isEdit = Boolean(initial?.id)
  const activePage = CHARACTER_FORM_PAGES[activePageIndex]
  const isFirstPage = activePageIndex === 0
  const isLastPage = activePageIndex === CHARACTER_FORM_PAGES.length - 1

  const getPanel = (pageKey: string) =>
    formRef.current?.querySelector<HTMLElement>(`[data-character-form-panel="${pageKey}"]`)

  const firstInvalidControl = (container?: ParentNode | null) =>
    Array.from(container?.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
      'input, select, textarea',
    ) ?? []).find((control) => control.willValidate && !control.validity.valid)

  const showPage = (pageIndex: number, focusHeading = true) => {
    const boundedIndex = Math.min(Math.max(pageIndex, 0), CHARACTER_FORM_PAGES.length - 1)
    const page = CHARACTER_FORM_PAGES[boundedIndex]
    setActivePageIndex(boundedIndex)
    if (focusHeading) {
      requestAnimationFrame(() => {
        getPanel(page.key)?.querySelector<HTMLElement>('[data-character-form-heading]')?.focus()
      })
    }
  }

  const validatePage = () => {
    const invalid = firstInvalidControl(getPanel(activePage.key))
    if (!invalid) return true
    invalid.focus()
    invalid.reportValidity()
    return false
  }

  const handleNext = () => {
    if (validatePage()) showPage(activePageIndex + 1)
  }

  const updateGroup = <K extends keyof CharacterDraft>(key: K, value: CharacterDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const renderSection = (section: CharacterSectionKey) => {
    switch (section) {
      case 'basic':
        return <BasicInfoSection key={section} data={draft} onChange={setDraft} />
      case 'ability_scores':
        return <AbilityScoresSection key={section} data={draft.ability_scores} onChange={(value) => updateGroup('ability_scores', value)} />
      case 'combat':
        return <CombatStatsSection key={section} data={draft.combat} onChange={(value) => updateGroup('combat', value)} />
      case 'general':
        return <GeneralSection key={section} data={draft.general} onChange={(value) => updateGroup('general', value)} />
      case 'spellcasting':
        return <SpellcastingSection key={section} data={draft.spellcasting} onChange={(value) => updateGroup('spellcasting', value)} />
      case 'currency':
        return <CurrencySection key={section} data={draft.currency} onChange={(value) => updateGroup('currency', value)} />
      case 'personality':
        return <PersonalitySection key={section} data={draft.personality} onChange={(value) => updateGroup('personality', value)} />
      case 'appearance':
        return <AppearanceSection key={section} data={draft.appearance} onChange={(value) => updateGroup('appearance', value)} />
      case 'background_details':
        return <BackgroundSection key={section} data={draft.background_details} onChange={(value) => updateGroup('background_details', value)} />
      default: {
        const key = section as CharacterListKey
        const config = ITEM_CONFIG_BY_KEY[key]
        if (!config) return null
        return (
          <ItemListEditor
            key={key}
            title={config.title}
            fields={config.fields}
            emptyItem={config.emptyItem}
            items={normalizeItemList(key, draft[key])}
            onChange={(items) => updateGroup(key, items)}
          />
        )
      }
    }
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!isLastPage) {
      handleNext()
      return
    }

    const invalid = firstInvalidControl(event.currentTarget)
    if (invalid) {
      const panel = invalid.closest<HTMLElement>('[data-character-form-panel]')
      const invalidPageIndex = CHARACTER_FORM_PAGES.findIndex((page) => page.key === panel?.dataset.characterFormPanel)
      if (invalidPageIndex >= 0) showPage(invalidPageIndex, false)
      requestAnimationFrame(() => {
        invalid.focus()
        invalid.reportValidity()
      })
      return
    }

    setError('')
    setSaving(true)
    try {
      const payload = toCharacterPayload(draft)
      const response = isEdit && initial?.id
        ? await charactersApi.update(initial.id, payload)
        : await charactersApi.create(payload)
      onSaved(response.character)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Something went wrong')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      ref={formRef}
      className="character-form character-form-wizard"
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
            return (
              <li key={page.key} className={`character-form-wizard__step-item is-${position}`}>
                <button
                  type="button"
                  className={`character-form-wizard__step-button${isActive ? ' is-active' : ''}`}
                  aria-current={isActive ? 'step' : undefined}
                  aria-controls={`character-form-panel-${page.key}`}
                  aria-label={`Step ${pageIndex + 1} of ${CHARACTER_FORM_PAGES.length}: ${page.label}`}
                  onClick={() => showPage(pageIndex)}
                >
                  <span className="character-form-wizard__step-number" aria-hidden="true">{pageIndex + 1}</span>
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
          const headingId = `character-form-heading-${page.key}`
          return (
            <section
              key={page.key}
              id={`character-form-panel-${page.key}`}
              className={`character-form-wizard__panel${isActive ? ' is-active' : ''}`}
              data-character-form-panel={page.key}
              aria-labelledby={headingId}
              hidden={!isActive}
            >
              <header className="character-form-wizard__panel-header">
                <p className="character-form-wizard__eyebrow">
                  Step {pageIndex + 1} of {CHARACTER_FORM_PAGES.length}
                </p>
                <h2 id={headingId} className="character-form-wizard__panel-title" data-character-form-heading tabIndex={-1}>
                  {page.label}
                </h2>
              </header>
              {page.sections.map(renderSection)}
            </section>
          )
        })}
      </div>

      <div className="character-form-wizard__actions">
        <div className="character-form-wizard__actions-start">
          <Button type="button" variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Button>
          {onToggleAI && (
            <Button type="button" variant={aiCollapsed ? 'primary' : 'secondary'} onClick={onToggleAI} disabled={saving}>
              <i className="bi bi-stars" aria-hidden="true" /> {aiCollapsed ? 'AI Help' : 'Hide AI'}
            </Button>
          )}
        </div>
        <div className="character-form-wizard__actions-end">
          <Button
            type="button"
            variant="secondary"
            disabled={isFirstPage || saving}
            onClick={() => showPage(activePageIndex - 1)}
          >
            <i className="bi bi-chevron-left" aria-hidden="true" /> Back
          </Button>
          {isLastPage ? (
            <Button type="submit" variant="primary" loading={saving}>
              {isEdit ? 'Save Changes' : 'Create Character'}
            </Button>
          ) : (
            <Button type="submit" variant="primary" disabled={saving}>
              Next <i className="bi bi-chevron-right" aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>
    </form>
  )
}
