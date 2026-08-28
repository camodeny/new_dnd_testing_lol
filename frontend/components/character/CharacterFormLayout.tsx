'use client'

import { useState } from 'react'
import CharacterFormPage from './CharacterFormPage'
import CharacterAIAssist from './CharacterAIAssist'
import type { CharacterDraft } from './characterFormConfig'
import type { Character } from '@/types'

interface Props {
  characterId: string
  initial?: Partial<Character>
  onSaved: (character: Character) => void
  onCancel: () => void
}

export default function CharacterFormLayout({ characterId, initial, onSaved, onCancel }: Props) {
  const [aiPatch, setAiPatch] = useState<Partial<CharacterDraft> | null>(null)
  const [aiCollapsed, setAiCollapsed] = useState(false)
  const [draftSnapshot, setDraftSnapshot] = useState<CharacterDraft | null>(null)
  const [activePage, setActivePage] = useState<string>('identity')

  return (
    <div className="page character-create-page has-sidebar">
      <div className={`character-create-layout ${aiCollapsed ? 'is-collapsed' : ''}`}>
        {!aiCollapsed && (
          <aside className="character-ai-sidebar">
            <div className="character-ai-sidebar__head">
              <span><i className="bi bi-stars" aria-hidden="true" /> AI Helper</span>
              <button type="button" className="character-ai-sidebar__close" onClick={() => setAiCollapsed(true)} aria-label="Close AI helper">
                <i className="bi bi-x-lg" aria-hidden="true" />
              </button>
            </div>
            <div className="character-ai-sidebar__content">
              <CharacterAIAssist
                characterId={characterId}
                draftCharacter={draftSnapshot as unknown as Record<string, unknown>}
                activePage={activePage}
                onGenerated={(draft) => setAiPatch({ ...draft } as Partial<CharacterDraft>)}
              />
              {aiPatch && (
                <div className="character-ai-banner">
                  <i className="bi bi-check-circle-fill" aria-hidden="true" /> Draft applied
                  <button type="button" className="link-button" onClick={() => setAiPatch(null)}>Clear</button>
                </div>
              )}
            </div>
          </aside>
        )}

        <div className="character-create-main">
          <CharacterFormPage
            initial={initial}
            aiPatch={aiPatch}
            onSaved={onSaved}
            onCancel={onCancel}
            onToggleAI={() => setAiCollapsed((v) => !v)}
            aiCollapsed={aiCollapsed}
            onDraftChange={setDraftSnapshot}
            onActivePageChange={setActivePage}
          />
        </div>
      </div>
    </div>
  )
}
