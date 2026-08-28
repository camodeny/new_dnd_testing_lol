'use client'

import { useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import MarkdownContent from '@/components/common/MarkdownContent'
import type { Campaign, Character, Session, Message, EncounterMap, User } from '@/types'

interface StoryAtlasProps {
  campaign: Campaign & { user_id?: string }
  characters: Character[]
  session: Session | null
  messages: Message[]
  hasOlderMessages: boolean
  currentUser: User | null
  currentCharacter: Character | null
  encounterMap: EncounterMap | null
  aiThinking: boolean
  aiThinkingStatus: string
  isOwner: boolean
  onSendMessage: (content: string) => Promise<void>
  onLoadOlderMessages: () => Promise<void>
  onStartSession: () => Promise<void>
  onEncounterMapChange: (map: EncounterMap | null) => void
  onExitToCampaigns: () => void
}

function getInitials(name: string): string {
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export default function StoryAtlas({
  campaign,
  characters,
  session,
  messages,
  hasOlderMessages,
  currentUser,
  currentCharacter,
  aiThinking,
  aiThinkingStatus,
  isOwner,
  onSendMessage,
  onLoadOlderMessages,
  onStartSession,
  onExitToCampaigns,
}: StoryAtlasProps) {
  const router = useRouter()
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [mobileTab, setMobileTab] = useState<'chat' | 'party'>('chat')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, aiThinking])

  const handleSend = async () => {
    const content = input.trim()
    if (!content || sending) return
    setInput('')
    setSending(true)
    try {
      await onSendMessage(content)
    } finally {
      setSending(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const initials = getInitials(campaign.name)

  return (
    <div className="dashboard-page">
      <div className={`dashboard-layout${mobileTab === 'party' ? ' mobile-tab-party' : ' mobile-tab-chat'}`}>
        {/* Left sidebar */}
        <aside className="dashboard-left">
          <div className="campaign-logo-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div className="campaign-logo" aria-hidden="true">{initials}</div>
              <div>
                <div className="campaign-logo-title">{campaign.name}</div>
                <div className="campaign-logo-subtitle">
                  {session ? 'Session active' : 'No session'}
                </div>
              </div>
            </div>
          </div>

          <nav className="sidebar-nav" aria-label="Campaign navigation">
            <div className="sidebar-nav-group">
              <div className="sidebar-nav-label">Navigation</div>
              <button type="button" className="sidebar-nav-item" onClick={onExitToCampaigns} style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', cursor: 'pointer' }}>
                <i className="bi bi-house" aria-hidden="true" /> Campaigns
              </button>
              <button type="button" className="sidebar-nav-item" onClick={() => router.push('/characters')} style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%', cursor: 'pointer' }}>
                <i className="bi bi-people" aria-hidden="true" /> Characters
              </button>
            </div>
          </nav>

          {/* Party roster */}
          {characters.length > 0 && (
            <div style={{ marginTop: 'auto', paddingTop: 18, borderTop: '1px solid var(--border-color)' }}>
              <div className="sidebar-nav-label">Party</div>
              {characters.map((c) => (
                <div
                  key={c.id}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 9,
                    padding: '8px 0', fontSize: '0.75rem', color: 'var(--text-muted)',
                  }}
                >
                  <div style={{
                    width: 28, height: 28, borderRadius: '50%',
                    background: 'var(--color-primary)', color: '#fff8ec',
                    display: 'grid', placeItems: 'center', fontSize: '0.6rem', fontWeight: 700, flexShrink: 0,
                  }}>
                    {getInitials(c.name)}
                  </div>
                  <div>
                    <div style={{ color: 'var(--text-bright)', fontWeight: 600 }}>{c.name}</div>
                    <div>{c.race}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </aside>

        {/* Center: chat */}
        <div className="dashboard-center">
          <div className="session-panel" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
            {/* Top header */}
            <header className="dashboard-top-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <div>
                <div className="location-name">{campaign.name}</div>
              </div>
              {!session && isOwner && (
                <button type="button" className="btn btn-primary small" onClick={onStartSession}>
                  <i className="bi bi-fire" aria-hidden="true" /> Start session
                </button>
              )}
            </header>

            {/* Messages */}
            <div className="session-messages" style={{ flex: 1, overflowY: 'auto', padding: '22px clamp(18px, 4.5vw, 72px) 0' }}>
              {hasOlderMessages && (
                <div style={{ textAlign: 'center', marginBottom: 16 }}>
                  <button
                    type="button"
                    className="btn btn-secondary small"
                    onClick={onLoadOlderMessages}
                  >
                    Load earlier messages
                  </button>
                </div>
              )}

              {messages.length === 0 && !aiThinking && (
                <div style={{ textAlign: 'center', padding: '80px 24px', color: 'var(--text-dim)' }}>
                  <div style={{ fontSize: '2rem', marginBottom: 12 }}>✦</div>
                  <p style={{ fontSize: '0.88rem', margin: 0 }}>
                    {session ? 'The adventure awaits your first move.' : 'Start a session to begin the adventure.'}
                  </p>
                </div>
              )}

              {messages.map((msg) => (
                <div
                  key={msg.id}
                  className="session-msg"
                  style={{ display: 'flex', alignItems: 'flex-start', gap: 12, maxWidth: 900, width: '100%', marginInline: 'auto' }}
                >
                  <div
                    className={`session-msg-avatar${msg.role === 'dm' ? ' dm-avatar' : ''}`}
                    aria-hidden="true"
                    style={{
                      width: 38, height: 38, borderRadius: '50%', flexShrink: 0,
                      border: '1px solid var(--border-color)',
                      background: msg.role === 'dm' ? '#542f24' : 'rgba(255,255,255,0.06)',
                      color: msg.role === 'dm' ? '#f6d6c6' : 'var(--text-muted)',
                      display: 'grid', placeItems: 'center', fontSize: '0.62rem', fontWeight: 700,
                    }}
                  >
                    {msg.role === 'dm' ? 'DM' : getInitials(msg.sender_name ?? currentUser?.username ?? '?')}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
                      <span className="session-msg-username" style={{ fontWeight: 700 }}>
                        {msg.role === 'dm' ? 'Dungeon Master' : (msg.sender_name ?? 'Player')}
                      </span>
                      <span className="session-msg-time" style={{ fontSize: '0.68rem' }}>
                        {formatTime(msg.created_at)}
                      </span>
                    </div>
                    <div className={`session-msg-content${msg.is_ic ? ' session-ic-message' : ''}`}>
                      {msg.role === 'dm' ? (
                        <MarkdownContent content={msg.content} />
                      ) : (
                        <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{msg.content}</p>
                      )}
                    </div>
                  </div>
                </div>
              ))}

              {aiThinking && (
                <div className="session-msg" style={{ display: 'flex', alignItems: 'flex-start', gap: 12, maxWidth: 900, width: '100%', marginInline: 'auto' }}>
                  <div style={{
                    width: 38, height: 38, borderRadius: '50%', flexShrink: 0,
                    border: '1px solid rgba(209, 107, 72, 0.34)',
                    background: '#542f24', color: '#f6d6c6',
                    display: 'grid', placeItems: 'center', fontSize: '0.62rem', fontWeight: 700,
                  }}>DM</div>
                  <div style={{ flex: 1, padding: '4px 0' }}>
                    <div style={{ fontWeight: 700, color: 'var(--text-bright)', marginBottom: 4, fontSize: '0.82rem' }}>Dungeon Master</div>
                    <div style={{ color: 'var(--text-dim)', fontSize: '0.84rem', display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span className="app-loading-mark" style={{ fontSize: '0.8rem' }}>✦</span>
                      {aiThinkingStatus || 'Thinking…'}
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Input area */}
            {session && (
              <div className="session-input-area" style={{ flexShrink: 0 }}>
                <div className="session-input-shell" style={{ display: 'flex', alignItems: 'flex-end', gap: 8, padding: '8px 10px' }}>
                  <textarea
                    className="session-input-editable"
                    placeholder="What do you do?"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    rows={1}
                    disabled={sending || aiThinking}
                    style={{
                      flex: 1, background: 'transparent', border: 'none', resize: 'none',
                      color: 'var(--text-bright)', fontSize: '0.9rem', lineHeight: 1.5,
                      maxHeight: '200px', overflowY: 'auto',
                    }}
                  />
                  <button
                    type="button"
                    className="session-send-btn"
                    onClick={handleSend}
                    disabled={!input.trim() || sending || aiThinking}
                    aria-label="Send message"
                  >
                    <i className="bi bi-send-fill" aria-hidden="true" />
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Right sidebar */}
        <aside className="dashboard-right">
          <div className="right-sidebar-widget" style={{ padding: '18px 0' }}>
            <div className="widget-header" style={{ marginBottom: 16 }}>
              <h3 style={{ margin: 0 }}>Party</h3>
            </div>
            {characters.length === 0 ? (
              <p style={{ color: 'var(--text-dim)', fontSize: '0.8rem' }}>No characters in this campaign yet.</p>
            ) : (
              <div style={{ display: 'grid', gap: 10 }}>
                {characters.map((c) => (
                  <div
                    key={c.id}
                    style={{
                      padding: 12, borderRadius: 8, border: '1px solid var(--border-color)',
                      background: 'rgba(255,255,255,0.025)',
                    }}
                  >
                    <div style={{ fontWeight: 700, color: 'var(--text-bright)', fontSize: '0.83rem', marginBottom: 3 }}>{c.name}</div>
                    <div style={{ color: 'var(--text-muted)', fontSize: '0.7rem' }}>
                      {[c.race, c.classes?.map((cl) => `${cl.class_name} ${cl.level}`).join('/')].filter(Boolean).join(' · ')}
                    </div>
                    {c.hit_points != null && (
                      <div style={{ marginTop: 6, color: 'var(--text-dim)', fontSize: '0.68rem' }}>
                        ❤ {c.hit_points} HP
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </aside>
      </div>

      {/* Mobile bottom nav */}
      <nav className="mobile-bottom-nav" aria-label="Mobile navigation">
        <button
          type="button"
          className={`mobile-nav-item${mobileTab === 'chat' ? ' active' : ''}`}
          onClick={() => setMobileTab('chat')}
        >
          <i className="bi bi-chat-text" aria-hidden="true" />
          <span>Chat</span>
        </button>
        <button
          type="button"
          className={`mobile-nav-item${mobileTab === 'party' ? ' active' : ''}`}
          onClick={() => setMobileTab('party')}
        >
          <i className="bi bi-people" aria-hidden="true" />
          <span>Party</span>
        </button>
      </nav>
    </div>
  )
}
