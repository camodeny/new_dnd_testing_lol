'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import CharacterFormPage from '@/components/character/CharacterFormPage'
import CharacterAIAssist from '@/components/character/CharacterAIAssist'
import type { CharacterDraft } from '@/components/character/characterFormConfig'

export default function CharacterCreatePage() {
  const router = useRouter()
  const [aiPatch, setAiPatch] = useState<Partial<CharacterDraft> | null>(null)
  const [aiCollapsed, setAiCollapsed] = useState(false)

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
              <CharacterAIAssist onGenerated={(draft) => setAiPatch({ ...draft } as Partial<CharacterDraft>)} />
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
            aiPatch={aiPatch}
            onSaved={(character) => router.push(`/characters/${character.id}`)}
            onCancel={() => router.push('/characters')}
            onToggleAI={() => setAiCollapsed((v) => !v)}
            aiCollapsed={aiCollapsed}
          />
        </div>
      </div>
    </div>
  )
}
