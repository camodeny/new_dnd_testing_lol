'use client'

import { useEffect, useState, useCallback, useMemo } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { useAuthContext } from '@/contexts/AuthContext'
import {
  campaigns as campaignsApi,
  campaignMembers,
  sessions as sessionsApi,
  apiFetch,
} from '@/lib/api'
import { useLiveTableRealtime } from '@/hooks/useLiveTableRealtime'
import { activeDmText, projectLiveTableMessages } from '@/lib/liveTableProjection'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import CampaignLobby from '@/components/dashboard/CampaignLobby'
import StoryAtlas from '@/components/dashboard/StoryAtlas'
import type { Campaign, Character, Session, EncounterMap } from '@/types'

type CampaignMode = 'lobby' | 'planning' | 'world-building' | 'session'

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

  const [campaign, setCampaign] = useState<(Campaign & { active_session?: Session | null; world?: unknown }) | null>(null)
  const [characters, setCharacters] = useState<Character[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null)
  const [encounterMap, setEncounterMap] = useState<EncounterMap | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<CampaignMode | null>(null)

  const currentCharacter = characters.find((c) => String(c.id) === String((user as { character_id?: number | string } | null)?.character_id)) ?? characters[0] ?? null
  const liveTable = useLiveTableRealtime({
    campaignId: id ? String(id) : null,
    threadId: activeThreadId,
    enabled: Boolean(session && activeThreadId),
  })
  const messages = useMemo(() => projectLiveTableMessages({
    submissions: liveTable.messages,
    dmMessages: liveTable.dmMessages,
    characters,
    currentUser: user,
    sessionId: session?.id,
  }), [liveTable.messages, liveTable.dmMessages, characters, user, session?.id])
  const streamingDmText = activeDmText(liveTable.dmState, liveTable.dmMessages)
  const aiThinking = Boolean(liveTable.dmState?.streaming || liveTable.dmStatus?.type === 'dm.thinking')
  const aiThinkingStatus = typeof liveTable.dmStatus?.status === 'string' ? liveTable.dmStatus.status : ''

  const loadData = useCallback(async () => {
    if (!id) return
    try {
      const [campData, charData, channelData] = await Promise.all([
        campaignsApi.get(String(id)) as Promise<{ campaign: Campaign & { active_session?: Session | null } }>,
        campaignMembers.listCharacters(String(id)),
        apiFetch<{ channels: Array<{ thread_id: string; thread_type: string }> }>(`/campaigns/${id}/realtime/channels`),
      ])

      const camp = campData.campaign
      setCampaign(camp)
      setCharacters(charData.characters ?? [])
      const campaignThread = channelData.channels.find((channel) => channel.thread_type === 'campaign')
      if (!campaignThread) throw new Error('The campaign live table is not available.')
      setActiveThreadId(campaignThread.thread_id)

      const activeSession = camp.active_session ?? null

      if (activeSession) {
        setSession(activeSession)
        // No authoritative encounter-map backend (stub removed); map state
        // stays client-owned via StoryAtlas onEncounterMapChange.
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

  const handleStartSession = useCallback(async () => {
    if (!id) return
    try {
      const data = await sessionsApi.start(String(id)) as { session: Session }
      setSession(data.session)
      setMode('session')
    } catch (err) {
      setError((err as Error).message)
    }
  }, [id])

  const handleSendMessage = useCallback(async (content: string) => {
    if (!id || !session?.id || !activeThreadId) return
    try {
      const idempotencyKey =
        typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`
      await apiFetch(`/campaigns/${id}/submissions`, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({
          content,
          thread_id: activeThreadId,
          character_id: currentCharacter?.id ?? null,
        }),
      })
      await liveTable.refresh()
    } catch (err) {
      setError((err as Error).message)
      throw err
    }
  }, [id, session?.id, activeThreadId, currentCharacter?.id, liveTable.refresh])

  const handleLoadOlderMessages = useCallback(async () => {
    await liveTable.loadOlder()
  }, [liveTable])

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

  const isOwner = campaign.owner_id === user?.id

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

  const liveError = liveTable.error
  const liveStatus = liveTable.phase
  const showTableLoadError = !liveTable.hasSnapshot && liveTable.phase === 'error' && !!liveError
  const combinedError = showTableLoadError ? liveError! : error

  return (
    <>
      {combinedError && (
        <div style={{
          position: 'fixed', top: 14, left: '50%', transform: 'translateX(-50%)',
          zIndex: 1200, width: 'min(540px, calc(100% - 32px))', margin: 0,
          borderColor: 'rgba(209, 107, 72, 0.36)', background: 'rgba(55, 29, 24, 0.96)',
          color: '#f4c6b6', boxShadow: '0 18px 44px rgba(0, 0, 0, 0.32)',
        }} className="error-message">
          {combinedError}
          {showTableLoadError && (
            <> <button type="button" className="btn btn-secondary small" onClick={() => void liveTable.refresh()} style={{ marginLeft: 8 }}>Retry</button></>
          )}
        </div>
      )}
      {showTableLoadError && liveTable.hasSnapshot === false ? (
        <div style={{ display: 'grid', placeItems: 'center', minHeight: '60vh', padding: 32, textAlign: 'center' }}>
          <div>
            <p style={{ color: 'var(--text-dim)', marginBottom: 16 }}>Live table failed to load.</p>
            <button type="button" className="btn btn-primary" onClick={() => void liveTable.refresh()}>Retry</button>
          </div>
        </div>
      ) : (
        <StoryAtlas
          campaign={campaign}
          characters={characters}
          session={session}
          messages={messages}
          hasOlderMessages={liveTable.hasOlderMessages}
          currentUser={user}
          currentCharacter={currentCharacter}
          encounterMap={encounterMap}
          aiThinking={aiThinking}
          aiThinkingStatus={aiThinkingStatus}
          activeDmText={streamingDmText}
          liveStatus={liveStatus}
          liveError={liveError}
          loadingOlderMessages={liveTable.loadingOlder}
          isOwner={isOwner}
          onSendMessage={handleSendMessage}
          onLoadOlderMessages={handleLoadOlderMessages}
          onRetryLiveTable={liveTable.refresh}
          onStartSession={handleStartSession}
          onEncounterMapChange={setEncounterMap}
          onExitToCampaigns={() => router.push('/')}
        />
      )}
    </>
  )
}
