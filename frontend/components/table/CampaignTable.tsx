'use client'

import { useEffect, useRef, useState, type ReactNode } from 'react'
import MarkdownContent from '@/components/common/MarkdownContent'
import IcOocText from '@/components/dashboard/IcOocText'
import PrivateThreadConversation from '@/components/dashboard/PrivateThreadConversation'
import CombatArena from '@/components/table/CombatArena'
import Composer, { composeForMode, type ComposerMode } from '@/components/table/Composer'
import RollCard from '@/components/table/RollCard'
import SidePanel from '@/components/table/SidePanel'
import { campaignMembers, gameplayThreads } from '@/lib/api'
import type { CapacityUiEvent } from '@/lib/capacity'
import { privateThreadLabel, upsertVisibleThread } from '@/lib/privateThreads'
import type { PlayerRollForRealtime } from '@/lib/realtime'
import {
  activeEncounter,
  buildTimeline,
  healthy,
  panelTabs,
  shopTab,
  type PanelTab,
  type TableCharacter,
  type TableProjection,
} from '@/lib/table'
import type { Campaign, CampaignMember, CampaignThread, Message, Session, User } from '@/types'

interface CampaignTableProps {
  campaign: Campaign
  session: Session | null
  messages: Message[]
  hasOlderMessages: boolean
  currentUser: User | null
  /** Player-lane table projection from the live-table snapshot. */
  table: TableProjection | null
  rollRequests: PlayerRollForRealtime[]
  aiThinking: boolean
  aiThinkingStatus: string
  /** DM status, retry, and other players' outstanding rolls. */
  turnControls?: ReactNode
  activeDmText?: string
  liveStatus?: 'idle' | 'loading' | 'live' | 'reconnecting' | 'reconciling' | 'error'
  liveError?: string | null
  loadingOlderMessages?: boolean
  isOwner: boolean
  /** Issue #255: new AI narration is paused on shared campaign capacity.
   *  Non-AI surfaces stay usable; the composer stays editable but
   *  submissions are held as a local draft. */
  aiPaused?: boolean
  /** Issue #255: quiet capacity meter / pause notice above the composer. */
  capacitySlot?: ReactNode
  /** Issue #255: telemetry for submission attempts made while paused. */
  onCapacityEvent?: (event: CapacityUiEvent) => void
  onSendMessage: (content: string) => Promise<void>
  onLoadOlderMessages: () => Promise<void>
  /** Resync from the authoritative snapshot (live-table retry, after rolls). */
  onRefresh?: () => Promise<unknown>
  onStartSession: () => Promise<void>
  onExitToCampaigns: () => void
}

export default function CampaignTable({
  campaign, session, messages, hasOlderMessages, currentUser, table, rollRequests,
  aiThinking, aiThinkingStatus, turnControls, activeDmText = '', liveStatus = 'idle',
  liveError = null, loadingOlderMessages = false, isOwner, aiPaused = false, capacitySlot,
  onCapacityEvent, onSendMessage, onLoadOlderMessages, onRefresh, onStartSession, onExitToCampaigns,
}: CampaignTableProps) {
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<ComposerMode>('do')
  const [sending, setSending] = useState(false)
  const [panelOpen, setPanelOpen] = useState(true)
  const [combatPanelOpen, setCombatPanelOpen] = useState(false)
  const [tab, setTab] = useState<PanelTab>('character')
  // Shops whose arrival card was already acted on this visit.
  const [browsedShops, setBrowsedShops] = useState<Set<string>>(new Set())
  const [switcherOpen, setSwitcherOpen] = useState(false)
  const [members, setMembers] = useState<CampaignMember[]>([])
  const [privateThreads, setPrivateThreads] = useState<CampaignThread[]>([])
  const [activePrivateThread, setActivePrivateThread] = useState<CampaignThread | null>(null)
  const [directParticipantId, setDirectParticipantId] = useState('')
  const [threadActionPending, setThreadActionPending] = useState(false)
  const [threadError, setThreadError] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesContainerRef = useRef<HTMLDivElement>(null)
  const lastItemRef = useRef<string | null>(null)

  const userId = currentUser?.id ?? null
  const multiplayer = (campaign.required_players ?? 1) > 1
  const encounter = activeEncounter(table)
  const inCombat = Boolean(encounter) && !activePrivateThread
  const party = healthy(table?.party) ?? []
  const character = healthy(table?.character) as TableCharacter | null
  const scene = healthy(table?.scene)
  const shops = inCombat ? [] : (table?.shops ?? [])
  const tabs = panelTabs(multiplayer, shops)
  const myRoll = rollRequests.find((roll) => roll.status === 'pending' && roll.requested_user_id === userId) ?? null
  const timeline = buildTimeline(messages, rollRequests, party, userId)

  // Narrow screens start story-first; the panel opens over the story.
  useEffect(() => {
    if (typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 900px)').matches) setPanelOpen(false)
  }, [])

  useEffect(() => {
    let cancelled = false
    Promise.all([gameplayThreads.list(campaign.id), campaignMembers.listMembers(campaign.id)])
      .then(([threadData, memberData]) => {
        if (cancelled) return
        setPrivateThreads(threadData.threads.filter((thread) => thread.thread_type === 'private'))
        setMembers(memberData.members)
      })
      .catch((error) => { if (!cancelled) setThreadError((error as Error).message) })
    return () => { cancelled = true }
  }, [campaign.id])

  useEffect(() => {
    const last = timeline.length ? timeline[timeline.length - 1].key : null
    if (last !== lastItemRef.current || activeDmText || myRoll) {
      messagesEndRef.current?.scrollIntoView({ behavior: lastItemRef.current ? 'smooth' : 'auto' })
    }
    lastItemRef.current = last
  }, [timeline.length, aiThinking, activeDmText, myRoll?.id])

  const openPrivateThread = (thread: CampaignThread) => {
    setActivePrivateThread(thread)
    setThreadError('')
    setSwitcherOpen(false)
  }

  const withThreadAction = async (action: () => Promise<CampaignThread>) => {
    setThreadActionPending(true)
    setThreadError('')
    try {
      const thread = await action()
      setPrivateThreads((current) => upsertVisibleThread(current, thread))
      openPrivateThread(thread)
    } catch (error) {
      setThreadError((error as Error).message)
    } finally {
      setThreadActionPending(false)
    }
  }

  const handleLoadOlder = async () => {
    const container = messagesContainerRef.current
    const previousHeight = container?.scrollHeight ?? 0
    await onLoadOlderMessages()
    requestAnimationFrame(() => {
      if (container) container.scrollTop += container.scrollHeight - previousHeight
    })
  }

  const handleSend = async () => {
    const text = draft.trim()
    if (!text || sending) return
    if (aiPaused) {
      // Paused: the draft stays editable and is never represented as
      // accepted/processing work. The attempt is tracked; nothing is sent.
      onCapacityEvent?.({ type: 'paused_submit_attempt' })
      return
    }
    const previous = draft
    setDraft('')
    setSending(true)
    try {
      await onSendMessage(composeForMode(text, mode))
    } catch {
      setDraft(previous)
    } finally {
      setSending(false)
    }
  }

  const suggest = (text: string) => {
    setMode('do')
    setDraft(text)
    requestAnimationFrame(() => {
      const ta = inputRef.current
      if (!ta) return
      ta.focus()
      ta.setSelectionRange(text.length, text.length)
    })
  }

  const thread = (
    <div className="tv-chat">
      <div ref={messagesContainerRef} className="tv-thread">
        <div className="tv-thread-inner">
          {(liveStatus === 'reconnecting' || liveStatus === 'reconciling') && (
            <p role="status" className="tv-note">
              {liveStatus === 'reconciling' ? 'Catching up with the table…' : 'Reconnecting to the table…'}
            </p>
          )}
          {liveError && messages.length > 0 && (
            <p role="alert" className="tv-note error">
              Live updates are unavailable. Your story so far is safe.{onRefresh && (
                <> <button type="button" className="tv-link" onClick={() => void onRefresh()}>Try again</button></>
              )}
            </p>
          )}
          {hasOlderMessages && (
            <div className="tv-older">
              <button type="button" className="tv-btn ghost small" onClick={handleLoadOlder} disabled={loadingOlderMessages}>
                {loadingOlderMessages ? 'Loading…' : 'Show earlier'}
              </button>
            </div>
          )}
          {timeline.length === 0 && !aiThinking && (
            <p className="tv-empty">{session ? 'The story is about to begin.' : 'Start a session to begin the story.'}</p>
          )}
          {timeline.map((item) => {
            if (item.kind === 'roll') {
              const { roll } = item
              return (
                <p className="tv-roll-line" key={item.key}>
                  {item.mine ? <>{roll.label} · you rolled <b>{roll.fulfillment?.total}</b></>
                    : <>{item.who} rolled <b>{roll.fulfillment?.total}</b> for {roll.label}</>}
                </p>
              )
            }
            const msg = item.message
            if (msg.role === 'dm') {
              return <div className="tv-dm" key={item.key}><MarkdownContent content={msg.content} /></div>
            }
            return (
              <div className={msg.is_own ? 'tv-you' : 'tv-other'} key={item.key}>
                {!msg.is_own && <div className="tv-other-name">{msg.sender_name ?? 'Player'}</div>}
                <div className={`tv-bubble${msg.is_ic ? ' said' : ''}`}><IcOocText message={msg} /></div>
              </div>
            )
          })}
          {(aiThinking || activeDmText) && (
            <div className="tv-dm streaming" aria-live="polite">
              {activeDmText && <MarkdownContent content={activeDmText} />}
              {aiThinking && <p className="tv-thinking">{aiThinkingStatus || (activeDmText ? 'Writing…' : 'The DM is thinking…')}</p>}
            </div>
          )}
          {shops.filter((shop) => !browsedShops.has(shop.entity_id)).map((shop) => (
            <div className="tv-card tv-place-card" key={shop.entity_id}>
              <span className="tv-place-glyph" aria-hidden="true"><i className="bi bi-shop" /></span>
              <div><b>{shop.name}</b>{shop.summary && <span>{shop.summary}</span>}</div>
              <button type="button" className="tv-btn ghost" onClick={() => {
                setBrowsedShops((current) => new Set([...current, shop.entity_id]))
                setTab(shopTab(shop))
                setPanelOpen(true)
              }}>Browse wares</button>
            </div>
          ))}
          {turnControls && <div className="tv-turn-controls">{turnControls}</div>}
          {myRoll && onRefresh && (
            <RollCard campaignId={campaign.id} roll={myRoll} modifier={character?.roll_modifiers?.[myRoll.id]} refresh={onRefresh} />
          )}
          <div ref={messagesEndRef} />
        </div>
      </div>
      {capacitySlot && <div className="tv-capacity">{capacitySlot}</div>}
      {session && (
        <div className="tv-composer-wrap">
          <Composer
            value={draft}
            onChange={setDraft}
            mode={mode}
            onModeChange={setMode}
            onSubmit={() => void handleSend()}
            inputRef={inputRef}
            disabled={sending || aiThinking}
            sendDisabled={!draft.trim() || sending || aiThinking || aiPaused}
            aiPaused={aiPaused}
            placeholder={inCombat ? 'Describe what you do…' : undefined}
          />
        </div>
      )}
    </div>
  )

  const showPanel = inCombat ? combatPanelOpen : panelOpen && !activePrivateThread
  const panel = (
    <SidePanel
      campaignId={campaign.id} userId={userId} tabs={tabs} tab={tabs.includes(tab) ? tab : 'character'}
      onTab={setTab} onClose={() => (inCombat ? setCombatPanelOpen(false) : setPanelOpen(false))}
      character={table ? table.character : null} party={table ? table.party : null}
      journal={table ? table.journal : null} members={members}
      shops={shops} onSuggest={suggest} onRefresh={onRefresh}
    />
  )

  return (
    <div className={`tv${inCombat ? ' combat' : ''}${showPanel ? ' panel-open' : ''}`}>
      <header className="tv-header">
        <button type="button" className="tv-icon-btn" onClick={onExitToCampaigns} aria-label="Back to campaigns">
          <i className="bi bi-chevron-left" aria-hidden="true" />
        </button>
        <div className="tv-place">
          <b>{scene?.location_name || campaign.name}</b>
          {scene?.fictional_time && <span>{scene.fictional_time}</span>}
        </div>
        {inCombat && <span className="tv-fight"><i className="bi bi-lightning-charge" aria-hidden="true" /> Fight</span>}
        <div className="tv-header-right">
          {!session && isOwner && (
            <button type="button" className="tv-btn primary small" onClick={onStartSession}>Start session</button>
          )}
          {multiplayer && (
            <div className="tv-switcher">
              <button
                type="button" className={`tv-switch-btn${switcherOpen ? ' open' : ''}`}
                aria-expanded={switcherOpen} aria-haspopup="menu" onClick={() => setSwitcherOpen((open) => !open)}
              >
                <i className={activePrivateThread ? 'bi bi-lock' : 'bi bi-people'} aria-hidden="true" />
                {activePrivateThread && userId ? privateThreadLabel(activePrivateThread, members, userId) : 'The table'}
                <i className="bi bi-chevron-down tv-chev" aria-hidden="true" />
              </button>
              {switcherOpen && (
                <div className="tv-menu" role="menu">
                  <button type="button" role="menuitem" className="tv-menu-item" onClick={() => { setActivePrivateThread(null); setSwitcherOpen(false) }}>
                    <span className="tv-face"><i className="bi bi-people" aria-hidden="true" /></span>
                    <span>The table<small>Everyone</small></span>
                    {!activePrivateThread && <i className="bi bi-check2 tv-check" aria-hidden="true" />}
                  </button>
                  {privateThreads.map((t) => (
                    <button type="button" role="menuitem" className="tv-menu-item" key={t.id} onClick={() => openPrivateThread(t)}>
                      <span className={`tv-face${t.private_kind === 'dm' ? ' dm' : ''}`}>{t.private_kind === 'dm' ? 'DM' : <i className="bi bi-person" aria-hidden="true" />}</span>
                      <span>
                        {userId ? privateThreadLabel(t, members, userId) : 'Private'}
                        <small>{t.private_kind === 'dm' ? 'Just you and the DM' : 'Private with a player'}</small>
                      </span>
                      {activePrivateThread?.id === t.id && <i className="bi bi-check2 tv-check" aria-hidden="true" />}
                    </button>
                  ))}
                  <hr />
                  {!privateThreads.some((t) => t.private_kind === 'dm') && (
                    <button type="button" role="menuitem" className="tv-menu-item muted" disabled={!userId || threadActionPending}
                      onClick={() => void withThreadAction(async () => (await gameplayThreads.getOrCreateDm(campaign.id)).thread)}>
                      <span className="tv-face plain"><i className="bi bi-plus" aria-hidden="true" /></span>Talk privately with the DM
                    </button>
                  )}
                  {userId && members.some((m) => m.user_id !== userId) && (
                    <div className="tv-menu-direct">
                      <select aria-label="Player for private conversation" value={directParticipantId} onChange={(e) => setDirectParticipantId(e.target.value)}>
                        <option value="">Talk privately with…</option>
                        {members.filter((m) => m.user_id !== userId).map((m) => <option value={m.user_id} key={m.user_id}>{m.username}</option>)}
                      </select>
                      <button type="button" className="tv-btn ghost small" disabled={!directParticipantId || threadActionPending}
                        onClick={() => void withThreadAction(async () => {
                          const { thread: created } = await gameplayThreads.getOrCreateDirect(campaign.id, directParticipantId)
                          setDirectParticipantId('')
                          return created
                        })}>Open</button>
                    </div>
                  )}
                  {threadError && <p className="tv-error" role="alert">{threadError}</p>}
                </div>
              )}
            </div>
          )}
          <button
            type="button" className={`tv-icon-btn${showPanel ? ' on' : ''}`} aria-pressed={showPanel}
            aria-label="Toggle side panel"
            onClick={() => (inCombat ? setCombatPanelOpen((v) => !v) : setPanelOpen((v) => !v))}
            disabled={Boolean(activePrivateThread)}
          >
            <i className="bi bi-layout-sidebar-reverse" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="tv-body">
        {activePrivateThread && userId ? (
          <div className="tv-private">
            <PrivateThreadConversation
              campaignId={campaign.id}
              thread={activePrivateThread}
              members={members}
              currentUser={currentUser!}
              onClose={() => setActivePrivateThread(null)}
              onError={setThreadError}
            />
          </div>
        ) : inCombat && encounter ? (
          <>
            <CombatArena encounter={encounter} userId={userId} character={character} onSuggest={suggest} />
            <div className="tv-side-chat">{thread}</div>
          </>
        ) : thread}
        {showPanel && panel}
      </div>
    </div>
  )
}
