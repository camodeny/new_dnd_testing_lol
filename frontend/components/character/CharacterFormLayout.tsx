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
  const [clearTrigger, setClearTrigger] = useState(0)

  const handleClearChat = async () => {
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
      const backendBase =
        (typeof process !== 'undefined' && (process.env.NEXT_PUBLIC_BACKEND_URL as string | undefined)) ||
        (typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'http://localhost:5889' : '')
      const url = backendBase
        ? `${backendBase.replace(/\/$/, '')}/api/characters/${encodeURIComponent(characterId)}/chat`
        : `/api/characters/${encodeURIComponent(characterId)}/chat`
      await fetch(url, { method: 'DELETE', headers: token ? { Authorization: `Bearer ${token}` } : {} })
    } catch {
      // ignore
    }
    setClearTrigger((n) => n + 1)
  }

  const handleCancel = async () => {
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
      const backendBase =
        (typeof process !== 'undefined' && (process.env.NEXT_PUBLIC_BACKEND_URL as string | undefined)) ||
        (typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'http://localhost:5889' : '')
      const url = backendBase
        ? `${backendBase.replace(/\/$/, '')}/api/characters/${encodeURIComponent(characterId)}/chat`
        : `/api/characters/${encodeURIComponent(characterId)}/chat`
      await fetch(url, { method: 'DELETE', headers: token ? { Authorization: `Bearer ${token}` } : {} })
    } catch {
      // ignore
    }
    onCancel()
  }

  return (
    <div className="page character-create-page has-sidebar">
      <div className={`character-create-layout ${aiCollapsed ? 'is-collapsed' : ''}`}>
        {!aiCollapsed && (
          <aside className="character-ai-sidebar">
            <div className="character-ai-sidebar__head">
              <span><i className="bi bi-stars" aria-hidden="true" /> AI Helper</span>
              <div style={{ display: 'flex', gap: 4 }}>
                <button type="button" className="character-ai-sidebar__close" onClick={handleClearChat} aria-label="Clear chat" title="Clear chat">
                  <i className="bi bi-trash" aria-hidden="true" />
                </button>
                <button type="button" className="character-ai-sidebar__close" onClick={() => setAiCollapsed(true)} aria-label="Close AI helper">
                  <i className="bi bi-x-lg" aria-hidden="true" />
                </button>
              </div>
            </div>
            <div className="character-ai-sidebar__content">
              <CharacterAIAssist
                characterId={characterId}
                draftCharacter={draftSnapshot as unknown as Record<string, unknown>}
                activePage={activePage}
                clearTrigger={clearTrigger}
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
            onCancel={handleCancel}
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
