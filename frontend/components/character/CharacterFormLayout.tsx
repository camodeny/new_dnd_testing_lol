'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import CharacterFormPage from './CharacterFormPage'
import CharacterAIAssist from './CharacterAIAssist'
import { toCharacterPayload, type CharacterDraft } from './characterFormConfig'
import type { Character } from '@/types'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import Button from '@/components/common/Button'
import { characters as charactersApi } from '@/lib/api'

interface Props {
  characterId: string
  initial?: Partial<Character>
  onSaved: (character: Character) => void
  onCancel: () => void
  onDraftCreated?: (character: Character) => void
  // Optional lobby campaign for party-aware creator advice (#244).
  campaignId?: string | null
}

const NEW_DRAFT_OPERATION_KEY = 'fireside:new-character-draft-operation'
type DraftSaveStatus = 'pending' | 'saving' | 'saved' | 'error'

export default function CharacterFormLayout({ characterId, initial, onSaved, onCancel, onDraftCreated, campaignId }: Props) {
  const isNewCharacter = characterId === 'new'
  const [createdDraft, setCreatedDraft] = useState<Character | null>(null)
  const [createDraftError, setCreateDraftError] = useState('')
  const [creatingDraft, setCreatingDraft] = useState(isNewCharacter && !initial)
  const createOperationKeyRef = useRef<string | null>(null)
  const [draftSaveStatus, setDraftSaveStatus] = useState<DraftSaveStatus>('saved')
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const debouncePendingRef = useRef(false)
  const saveChainRef = useRef<Promise<void>>(Promise.resolve())
  const lastSaveFingerprintRef = useRef<string | null>(null)
  const latestSaveRef = useRef<{ characterId: string | number; payload: Record<string, unknown>; fingerprint: string } | null>(null)
  const pageHideSaveFingerprintRef = useRef<string | null>(null)
  const draftCompletedRef = useRef(false)
  const resolvedInitial = initial ?? createdDraft ?? undefined
  const activeCharacterId = resolvedInitial?.id ?? characterId
  const isDraft = resolvedInitial?.status === 'draft'

  const createDraft = useCallback(async () => {
    setCreatingDraft(true)
    setCreateDraftError('')
    try {
      const operationKey = createOperationKeyRef.current ||
        window.sessionStorage.getItem(NEW_DRAFT_OPERATION_KEY) ||
        window.crypto.randomUUID()
      createOperationKeyRef.current = operationKey
      window.sessionStorage.setItem(NEW_DRAFT_OPERATION_KEY, operationKey)
      const response = await charactersApi.createDraft(operationKey)
      if (window.sessionStorage.getItem(NEW_DRAFT_OPERATION_KEY) === operationKey) {
        window.sessionStorage.removeItem(NEW_DRAFT_OPERATION_KEY)
      }
      onDraftCreated?.(response.character)
      setCreatedDraft(response.character)
    } catch (error) {
      setCreateDraftError(error instanceof Error ? error.message : 'Could not start a character draft.')
    } finally {
      setCreatingDraft(false)
    }
  }, [onDraftCreated])

  useEffect(() => {
    if (isNewCharacter && !initial && !createdDraft) void createDraft()
  }, [createDraft, createdDraft, initial, isNewCharacter])

  const [aiPatch, setAiPatch] = useState<Partial<CharacterDraft> | null>(null)
  const [aiCollapsed, setAiCollapsed] = useState(false)
  const [draftSnapshot, setDraftSnapshot] = useState<CharacterDraft | null>(null)
  const [activePage, setActivePage] = useState<string>('identity')
  const [clearTrigger, setClearTrigger] = useState(0)

  const queueDraftSave = useCallback((payload: Record<string, unknown>, fingerprint: string) => {
    const savePromise = saveChainRef.current
      .catch(() => undefined)
      .then(async () => {
        await charactersApi.updateDraft(activeCharacterId, payload)
        lastSaveFingerprintRef.current = fingerprint
        if (latestSaveRef.current?.fingerprint === fingerprint) setDraftSaveStatus('saved')
      })
      .catch(() => {
        if (latestSaveRef.current?.fingerprint === fingerprint) setDraftSaveStatus('error')
      })
    saveChainRef.current = savePromise
    return savePromise
  }, [activeCharacterId])

  const flushDraftSave = useCallback(async () => {
    const latest = latestSaveRef.current
    if (!isDraft || !latest || lastSaveFingerprintRef.current === latest.fingerprint) return true
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current)
      saveTimerRef.current = null
      debouncePendingRef.current = false
    }
    setDraftSaveStatus('saving')
    await queueDraftSave(latest.payload, latest.fingerprint)
    return lastSaveFingerprintRef.current === latest.fingerprint
  }, [isDraft, queueDraftSave])

  const retryDraftSave = useCallback(() => {
    const latest = latestSaveRef.current
    if (!isDraft || !latest) return
    setDraftSaveStatus('saving')
    queueDraftSave(latest.payload, latest.fingerprint)
  }, [isDraft, queueDraftSave])

  useEffect(() => {
    if (!isDraft || !draftSnapshot) return
    const payload = { ...toCharacterPayload(draftSnapshot), creator_step: activePage }
    const fingerprint = JSON.stringify(payload)
    latestSaveRef.current = { characterId: activeCharacterId, payload, fingerprint }
    if (lastSaveFingerprintRef.current === fingerprint) return
    setDraftSaveStatus('pending')
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    debouncePendingRef.current = true
    saveTimerRef.current = setTimeout(() => {
      saveTimerRef.current = null
      debouncePendingRef.current = false
      setDraftSaveStatus('saving')
      queueDraftSave(payload, fingerprint)
    }, 650)
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    }
  }, [activePage, draftSnapshot, isDraft, queueDraftSave])

  useEffect(() => () => {
    const debounceWasPending = debouncePendingRef.current
    debouncePendingRef.current = false
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current)
      saveTimerRef.current = null
    }
    const latest = latestSaveRef.current
    if (
      !debounceWasPending ||
      draftCompletedRef.current ||
      !latest ||
      lastSaveFingerprintRef.current === latest.fingerprint ||
      pageHideSaveFingerprintRef.current === latest.fingerprint
    ) return
    pageHideSaveFingerprintRef.current = latest.fingerprint
    void charactersApi.updateDraft(latest.characterId, latest.payload, { keepalive: true })
      .then(() => { lastSaveFingerprintRef.current = latest.fingerprint })
      .catch(() => { pageHideSaveFingerprintRef.current = null })
  }, [])

  useEffect(() => {
    if (!isDraft) return
    const saveBeforePageHide = () => {
      const latest = latestSaveRef.current
      if (!latest || lastSaveFingerprintRef.current === latest.fingerprint || pageHideSaveFingerprintRef.current === latest.fingerprint) return
      pageHideSaveFingerprintRef.current = latest.fingerprint
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current)
        saveTimerRef.current = null
        debouncePendingRef.current = false
      }
      void charactersApi.updateDraft(activeCharacterId, latest.payload, { keepalive: true })
        .then(() => { lastSaveFingerprintRef.current = latest.fingerprint })
        .catch(() => { pageHideSaveFingerprintRef.current = null })
    }
    window.addEventListener('pagehide', saveBeforePageHide)
    return () => window.removeEventListener('pagehide', saveBeforePageHide)
  }, [activeCharacterId, isDraft])

  const handleSaved = useCallback((character: Character) => {
    draftCompletedRef.current = true
    onSaved(character)
  }, [onSaved])

  const clearChat = async () => {
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
      const backendBase =
        (typeof process !== 'undefined' && (process.env.NEXT_PUBLIC_BACKEND_URL as string | undefined)) ||
        (typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'http://localhost:5889' : '')
      const url = backendBase
        ? `${backendBase.replace(/\/$/, '')}/api/characters/${encodeURIComponent(activeCharacterId)}/chat`
        : `/api/characters/${encodeURIComponent(activeCharacterId)}/chat`
      await fetch(url, { method: 'DELETE', headers: token ? { Authorization: `Bearer ${token}` } : {} })
    } catch {
      // ignore
    }
  }

  const handleClearChat = async () => {
    await clearChat()
    setClearTrigger((n) => n + 1)
  }

  const handleCancel = async () => {
    const saved = await flushDraftSave()
    if (!saved) return
    await clearChat()
    onCancel()
  }

  if (isNewCharacter && !resolvedInitial) {
    return (
      <div className="page character-create-page character-draft-start">
        {createDraftError ? (
          <div className="character-draft-start__message">
            <ErrorMessage message={createDraftError} />
            <Button type="button" variant="primary" onClick={() => void createDraft()} disabled={creatingDraft}>
              Retry
            </Button>
            <Button type="button" variant="secondary" onClick={onCancel} disabled={creatingDraft}>
              Back to characters
            </Button>
          </div>
        ) : (
          <Loading />
        )}
      </div>
    )
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
                characterId={activeCharacterId}
                draftCharacter={draftSnapshot as unknown as Record<string, unknown>}
                activePage={activePage}
                clearTrigger={clearTrigger}
                campaignId={campaignId ?? null}
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
            initial={resolvedInitial}
            aiPatch={aiPatch}
            onSaved={handleSaved}
            onCancel={handleCancel}
            onToggleAI={() => setAiCollapsed((v) => !v)}
            onOpenAI={() => setAiCollapsed(false)}
            aiCollapsed={aiCollapsed}
            onDraftChange={setDraftSnapshot}
            onActivePageChange={setActivePage}
            draftSaveStatus={isDraft ? draftSaveStatus : undefined}
            onRetryDraftSave={retryDraftSave}
          />
        </div>
      </div>
    </div>
  )
}
