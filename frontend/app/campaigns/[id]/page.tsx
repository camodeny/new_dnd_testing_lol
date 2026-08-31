'use client'

import { useEffect, useState, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { useAuthContext } from '@/contexts/AuthContext'
import {
  campaigns as campaignsApi,
  campaignMembers,
  sessions as sessionsApi,
  legacyLiveTable,
  encounterMaps,
} from '@/lib/api'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import CampaignLobby from '@/components/dashboard/CampaignLobby'
import StoryAtlas from '@/components/dashboard/StoryAtlas'
import type { Campaign, Character, Session, Message, EncounterMap } from '@/types'

type CampaignMode = 'lobby' | 'planning' | 'world-building' | 'session'

const SESSION_MESSAGE_PAGE_SIZE = 50

function determineCampaignMode(
  campaign: Campaign & { active_session?: Session | null; world?: unknown },
  currentMode: CampaignMode | null,
): CampaignMode {
  if (currentMode) return currentMode
  if (campaign.active_session) return 'session'
  return 'lobby'
}

export default function CampaignViewPage() {
  const { id } = useParams<{ id: string }>()
  const { user } = useAuthContext()
  const router = useRouter()

  const [campaign, setCampaign] = useState<(Campaign & { active_session?: Session | null; world?: unknown; user_id?: string }) | null>(null)
  const [characters, setCharacters] = useState<Character[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [hasOlderMessages, setHasOlderMessages] = useState(false)
  const [encounterMap, setEncounterMap] = useState<EncounterMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<CampaignMode | null>(null)
  const [aiThinking, setAiThinking] = useState(false)
  const [aiThinkingStatus, setAiThinkingStatus] = useState('')

  const loadData = useCallback(async () => {
    if (!id) return
    try {
      const [campData, charData] = await Promise.all([
        campaignsApi.get(String(id)) as Promise<{ campaign: Campaign & { active_session?: Session | null; user_id?: string } }>,
        campaignMembers.listCharacters(String(id)),
      ])

      const camp = campData.campaign
      setCampaign(camp)
      setCharacters(charData.characters ?? [])

      const activeSession = camp.active_session ?? null

      if (activeSession) {
        setSession(activeSession)
        const [sessionData, mapData] = await Promise.all([
          legacyLiveTable.get(activeSession.id, { limit: SESSION_MESSAGE_PAGE_SIZE }).catch(() => ({ session: null, messages: [] })),
          encounterMaps.getCurrent(String(id)).catch(() => ({ map: null })),
        ])
        setMessages((sessionData as { messages?: Message[] }).messages ?? [])
        setHasOlderMessages(Boolean((sessionData as { has_more_messages?: boolean }).has_more_messages))
        setEncounterMap((mapData as { map: EncounterMap | null }).map)
        setMode('session')
      } else {
        setMode((prev) => determineCampaignMode(camp as Campaign & { active_session?: Session | null; world?: unknown }, prev))
      }
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    loadData()
  }, [loadData])

  // SSE stream for live messages during a session
  useEffect(() => {
    if (!session?.id) return
    const url = legacyLiveTable.streamUrl(session.id)
    const evtSource = new EventSource(url)

    evtSource.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data)
        if (payload.type === 'message') {
          setMessages((prev) => {
            const exists = prev.some((m) => m.id === payload.message?.id)
            return exists ? prev : [...prev, payload.message]
          })
          setAiThinking(false)
          setAiThinkingStatus('')
        } else if (payload.type === 'thinking') {
          setAiThinking(true)
          setAiThinkingStatus(payload.status ?? '')
        } else if (payload.type === 'done') {
          setAiThinking(false)
          setAiThinkingStatus('')
        }
      } catch { /* non-JSON ping */ }
    }

    evtSource.onerror = () => {
      setAiThinking(false)
    }

    return () => evtSource.close()
  }, [session?.id])

  const handleStartSession = useCallback(async () => {
    if (!id) return
    try {
      const data = await sessionsApi.start(String(id)) as { session: Session }
      setSession(data.session)
      setMessages([])
      setMode('session')
    } catch (err) {
      setError((err as Error).message)
    }
  }, [id])

  const handleSendMessage = useCallback(async (content: string) => {
    if (!session?.id) return
    try {
      await legacyLiveTable.sendMessage(session.id, content)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [session?.id])

  const handleLoadOlderMessages = useCallback(async () => {
    if (!session?.id || !hasOlderMessages) return
    const oldest = messages[0]
    if (!oldest) return
    try {
      const data = await legacyLiveTable.getMessages(session.id, {
        limit: SESSION_MESSAGE_PAGE_SIZE,
        beforeId: Number(oldest.id),
      })
      const older = (data as { messages?: Message[] }).messages ?? []
      if (older.length) {
        setMessages((prev) => {
          const existing = new Set(prev.map((m) => m.id))
          return [...older.filter((m) => !existing.has(m.id)), ...prev]
        })
      }
    } catch (err) {
      setError((err as Error).message)
    }
  }, [session?.id, hasOlderMessages, messages])

  // Genuine solo campaigns (required_players <= 1) skip the multiplayer holding page
  // Must be before any early returns to preserve hook order
  const isSolo = !loading && campaign ? (campaign.required_players ?? 1) <= 1 : false

  useEffect(() => {
    if (mode === 'planning' && isSolo && !session) {
      setMode('world-building')
    }
  }, [mode, isSolo, session])

  if (loading) return <Loading message="Loading campaign…" />
  if (error && !campaign) return <ErrorMessage message={error} />
  if (!campaign) return <ErrorMessage message="Campaign not found." />

  const isOwner = (campaign.owner_id ?? campaign.user_id) === user?.id
  const currentCharacter = characters.find((c) => String(c.id) === String((user as { character_id?: number | string } | null)?.character_id)) ?? characters[0] ?? null

  if (mode === 'lobby') {
    return (
      <>
        {error && <ErrorMessage message={error} style={{ position: 'fixed', top: 18, left: '50%', transform: 'translateX(-50%)', zIndex: 1500, width: 'min(520px, calc(100% - 32px))' }} />}
        <CampaignLobby
          campaign={campaign}
          currentUser={user}
          isOwner={isOwner}
          onBegin={() => setMode(isSolo ? 'world-building' : 'planning')}
        />
      </>
    )
  }

  if (mode === 'planning' && !session) {
    return (
      <div className="planning-page" style={{ justifyContent: 'center', position: 'relative' }}>
        <button
          type="button"
          className="dashboard-back"
          onClick={() => setMode('lobby')}
          aria-label="Back to lobby"
          style={{ position: 'absolute', top: 16, left: 16, zIndex: 2 }}
        >
          <i className="bi bi-arrow-left" aria-hidden="true" />
        </button>
        <div style={{ display: 'grid', placeItems: 'center', padding: 'clamp(24px, 4vw, 48px)', width: '100%' }}>
          <div style={{ textAlign: 'center', maxWidth: 480 }}>
            <p style={{ margin: '0 0 8px', color: 'var(--ember-hover)', font: '700 0.62rem/1 var(--mono)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>
              {campaign.name} — Planning
            </p>
            <div className="planning-wait-icon" style={{ margin: '0 auto 24px', width: 54, height: 54, display: 'grid', placeItems: 'center', borderRadius: '50%', background: 'var(--ember-soft)', color: 'var(--ember-hover)', fontSize: '1.15rem' }}>
              <i className="bi bi-people" aria-hidden="true" />
            </div>
            <h2 style={{ margin: '0 0 12px', fontSize: 'clamp(2rem, 4vw, 2.8rem)', letterSpacing: '-0.05em' }}>
              Ready your party
            </h2>
            <p style={{ color: 'var(--ink-muted)', marginBottom: 24 }}>
              Select characters, coordinate with your party, and prepare for adventure.
            </p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setMode('world-building')}
            >
              Continue <i className="bi bi-chevron-right" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
    )
  }

  if (mode === 'world-building' && !session) {
    const canGoBack = !isSolo
    return (
      <div className="planning-page" style={{ justifyContent: 'center', position: 'relative' }}>
        {canGoBack ? (
          <button
            type="button"
            className="dashboard-back"
            onClick={() => setMode('planning')}
            aria-label="Back to planning"
            style={{ position: 'absolute', top: 16, left: 16, zIndex: 2 }}
          >
            <i className="bi bi-arrow-left" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            className="dashboard-back"
            onClick={() => setMode('lobby')}
            aria-label="Back to lobby"
            style={{ position: 'absolute', top: 16, left: 16, zIndex: 2 }}
          >
            <i className="bi bi-arrow-left" aria-hidden="true" />
          </button>
        )}
        <div style={{ display: 'grid', placeItems: 'center', padding: 'clamp(24px, 4vw, 48px)', width: '100%' }}>
          <div style={{ textAlign: 'center', maxWidth: 480 }}>
            <p style={{ margin: '0 0 8px', color: 'var(--ember-hover)', font: '700 0.62rem/1 var(--mono)', letterSpacing: '0.12em', textTransform: 'uppercase' }}>
              {campaign.name} — World building
            </p>
            <div className="planning-wait-icon" style={{ margin: '0 auto 24px', width: 54, height: 54, display: 'grid', placeItems: 'center', borderRadius: '50%', background: 'var(--ember-soft)', color: 'var(--ember-hover)', fontSize: '1.15rem' }}>
              <i className="bi bi-globe2" aria-hidden="true" />
            </div>
            <h2 style={{ margin: '0 0 12px', fontSize: 'clamp(2rem, 4vw, 2.8rem)', letterSpacing: '-0.05em' }}>
              Light the fire
            </h2>
            <p style={{ color: 'var(--ink-muted)', marginBottom: 24 }}>
              The AI DM will open a new session and begin your adventure.
            </p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleStartSession}
            >
              <i className="bi bi-fire" aria-hidden="true" /> Begin adventure
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <>
      {error && (
        <div style={{
          position: 'fixed', top: 14, left: '50%', transform: 'translateX(-50%)',
          zIndex: 1200, width: 'min(540px, calc(100% - 32px))', margin: 0,
          borderColor: 'rgba(209, 107, 72, 0.36)', background: 'rgba(55, 29, 24, 0.96)',
          color: '#f4c6b6', boxShadow: '0 18px 44px rgba(0, 0, 0, 0.32)',
        }} className="error-message">
          {error}
        </div>
      )}
      <StoryAtlas
        campaign={campaign}
        characters={characters}
        session={session}
        messages={messages}
        hasOlderMessages={hasOlderMessages}
        currentUser={user}
        currentCharacter={currentCharacter}
        encounterMap={encounterMap}
        aiThinking={aiThinking}
        aiThinkingStatus={aiThinkingStatus}
        isOwner={isOwner}
        onSendMessage={handleSendMessage}
        onLoadOlderMessages={handleLoadOlderMessages}
        onStartSession={handleStartSession}
        onEncounterMapChange={setEncounterMap}
        onExitToCampaigns={() => router.push('/')}
      />
    </>
  )
}
