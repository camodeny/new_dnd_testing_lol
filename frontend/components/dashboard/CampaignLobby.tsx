'use client'

import { useState, useCallback, useEffect } from 'react'
import { campaignMembers as membersApi, characters as charactersApi } from '@/lib/api'
import type { Campaign, CampaignMember, Character, LobbyEligibility, User } from '@/types'

interface CampaignLobbyProps {
  campaign: Campaign
  currentUser: User | null
  isOwner: boolean
  onBegin: () => void
}

function newKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export default function CampaignLobby({ campaign, currentUser, isOwner, onBegin }: CampaignLobbyProps) {
  const [members, setMembers] = useState<CampaignMember[]>([])
  const [eligibility, setEligibility] = useState<LobbyEligibility | null>(null)
  const [revision, setRevision] = useState<number>(campaign.revision)
  const [launchLocked, setLaunchLocked] = useState(false)
  const [invite, setInvite] = useState<{ code?: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [owned, setOwned] = useState<Character[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [busy, setBusy] = useState(false)
  const [lobbyError, setLobbyError] = useState('')

  const refreshLobby = useCallback(async () => {
    try {
      const data = await membersApi.getLobby(campaign.id)
      setMembers(data.members ?? [])
      setEligibility(data.eligibility ?? null)
      setRevision(data.campaign?.revision ?? revision)
      setLaunchLocked(Boolean(data.launch_locked))
      setLobbyError('')
    } catch {
      // Fall back to members-only projection if lobby endpoint is unavailable
      try {
        const data = await membersApi.listMembers(campaign.id)
        setMembers((data as { members?: CampaignMember[] }).members ?? [])
      } catch { /* no-op */ }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaign.id])

  useEffect(() => {
    void refreshLobby()
  }, [refreshLobby])

  useEffect(() => {
    charactersApi
      .list()
      .then((data) => setOwned((data as { characters?: Character[] }).characters ?? []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!isOwner) return
    membersApi
      .getInvite(campaign.id)
      .then((data) => setInvite(data as { code?: string }))
      .catch(() => {})
  }, [campaign.id, isOwner])

  const handleGenerateCode = useCallback(async () => {
    try {
      const data = await membersApi.createInvite(campaign.id)
      setInvite(data as { code?: string })
    } catch { /* no-op */ }
  }, [campaign.id])

  const handleCopyCode = useCallback(async () => {
    if (!invite?.code) return
    await navigator.clipboard.writeText(invite.code).catch(() => {})
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [invite?.code])

  const me = members.find((m) => m.user_id === currentUser?.id) ?? null
  const myCharId = me?.selected_character_id ?? me?.character_id ?? null

  useEffect(() => {
    if (myCharId && !selectedId) setSelectedId(myCharId)
  }, [myCharId, selectedId])

  const handleSelect = useCallback(async () => {
    if (!selectedId || busy) return
    setBusy(true)
    setLobbyError('')
    try {
      await membersApi.selectCharacter(campaign.id, revision, selectedId, newKey())
      await refreshLobby()
    } catch (err) {
      setLobbyError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [selectedId, busy, campaign.id, revision, refreshLobby])

  const handleReadiness = useCallback(async (ready: boolean) => {
    if (busy) return
    setBusy(true)
    setLobbyError('')
    try {
      await membersApi.setReadiness(campaign.id, revision, ready, newKey())
      await refreshLobby()
    } catch (err) {
      setLobbyError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [busy, campaign.id, revision, refreshLobby])

  const filledSlots = members.length
  const totalSlots = (campaign as { required_players?: number }).required_players ?? members.length
  const canBegin = isOwner && (eligibility?.eligible ?? false)

  return (
    <div className="lobby-page">
      <div className="lobby-container">
        <div className="lobby-card">
          {/* Left: hero panel */}
          <div className="lobby-hero">
            <h1 className="lobby-title">{campaign.name}</h1>
            {campaign.description && (
              <p className="lobby-desc">{campaign.description}</p>
            )}
            {campaign.random_seed && (
              <div className="lobby-hero-tags">
                <span className="lobby-seed-tag">Seed: {campaign.random_seed}</span>
              </div>
            )}
          </div>

          {/* Character selection / readiness (solo dogfood path) */}
          <section className="lobby-invite-section" aria-label="Your character">
            <div className="lobby-section-header">
              <span className="lobby-section-label">
                <i className="bi bi-person-badge" aria-hidden="true" /> Your character
              </span>
              {me && (
                <span style={{ fontSize: '0.72rem', color: 'var(--ink-muted)' }}>
                  {me.is_ready ? 'Ready' : 'Not ready'}
                  {typeof me.character_progress?.percent === 'number' && ` · ${me.character_progress.percent}%`}
                </span>
              )}
            </div>
            <div className="lobby-invite-card">
              {launchLocked ? (
                <p className="lobby-invite-desc">Launch characters are locked — the campaign has started.</p>
              ) : (
                <>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <select
                      aria-label="Select your character"
                      value={selectedId}
                      onChange={(e) => setSelectedId(e.target.value)}
                      disabled={busy || owned.length === 0}
                      style={{ flex: '1 1 220px', padding: '8px 10px', borderRadius: 8 }}
                    >
                      <option value="">{owned.length ? 'Choose a character…' : 'No characters yet — create one first'}</option>
                      {owned.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}</option>
                      ))}
                    </select>
                    <button type="button" className="lobby-generate-btn" onClick={handleSelect} disabled={busy || !selectedId}>
                      Select
                    </button>
                    {me?.is_ready ? (
                      <button type="button" className="lobby-copy-btn" onClick={() => void handleReadiness(false)} disabled={busy}>
                        Unready
                      </button>
                    ) : (
                      <button type="button" className="lobby-copy-btn" onClick={() => void handleReadiness(true)} disabled={busy || !myCharId}>
                        Mark ready
                      </button>
                    )}
                  </div>
                  {me && (me.character_missing?.length ?? 0) > 0 && (
                    <p className="lobby-invite-hint">Incomplete: missing {me.character_missing!.join(', ')}.</p>
                  )}
                  {lobbyError && (
                    <p className="lobby-invite-hint" role="alert">{lobbyError}</p>
                  )}
                </>
              )}
            </div>
          </section>

          {/* Right: party section */}
          <section className="lobby-party-section">
            <div className="lobby-section-header">
              <span className="lobby-section-label">
                <i className="bi bi-people-fill" aria-hidden="true" /> The Party
              </span>
              <div className="lobby-size-control">
                <div className="lobby-size-display">
                  {filledSlots} / {totalSlots} player{totalSlots !== 1 ? 's' : ''}
                </div>
              </div>
            </div>

            <div className="lobby-players-grid">
              {members.map((member) => {
                const charId = member.selected_character_id ?? member.character_id ?? null
                return (
                  <div
                    key={member.user_id}
                    className={`lobby-player-slot lobby-slot-filled`}
                  >
                    <div className="lobby-slot-circle">
                      {member.username?.slice(0, 2).toUpperCase() ?? '?'}
                    </div>
                    <div className={`lobby-slot-name${member.user_id === currentUser?.id ? ' lobby-slot-you' : ''}`}>
                      {member.username}
                      {member.user_id === currentUser?.id && ' (you)'}
                    </div>
                    {member.character_name && (
                      <div className="lobby-slot-role">{member.character_name}</div>
                    )}
                    <div style={{ fontSize: '0.7rem', color: member.is_ready ? 'var(--ember-hover)' : 'var(--ink-faint)' }}>
                      {member.is_ready ? 'Ready' : charId ? 'Not ready' : 'No character'}
                    </div>
                    {member.role === 'owner' && (
                      <span style={{ position: 'absolute', top: 6, right: 6, fontSize: '0.55rem', fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', padding: '3px 6px', borderRadius: 4, background: 'var(--ember-soft)', color: 'var(--ember-hover)' }}>Host</span>
                    )}
                  </div>
                )
              })}

              {/* Empty slots */}
              {Array.from({ length: Math.max(0, totalSlots - filledSlots) }).map((_, i) => (
                <div key={`empty-${i}`} className="lobby-player-slot lobby-slot-empty">
                  <div className="lobby-slot-circle lobby-slot-circle-empty">
                    <i className="bi bi-person" aria-hidden="true" />
                  </div>
                  <div style={{ color: 'var(--ink-faint)', fontSize: '0.74rem' }}>Empty seat</div>
                </div>
              ))}
            </div>

            {/* Fill bar */}
            <div className="lobby-slots-status">
              <div className="lobby-slots-bar">
                <div
                  className="lobby-slots-fill"
                  style={{ width: `${Math.min(100, (filledSlots / Math.max(1, totalSlots)) * 100)}%` }}
                />
              </div>
              <span className="lobby-slots-text">
                {filledSlots} of {totalSlots} seat{totalSlots !== 1 ? 's' : ''} filled
              </span>
            </div>
            {eligibility && !eligibility.eligible && (
              <ul style={{ marginTop: 8, paddingLeft: 18, color: 'var(--ink-muted)', fontSize: '0.78rem' }}>
                {eligibility.blockers.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            )}
          </section>

          {/* Invite section */}
          {isOwner && (
            <section className="lobby-invite-section">
              <div className="lobby-section-header">
                <span className="lobby-section-label">
                  <i className="bi bi-link-45deg" aria-hidden="true" /> Invite players
                </span>
              </div>
              <div className="lobby-invite-card">
                <p className="lobby-invite-desc">Share this code with friends. They can join at any time using the &quot;Join with code&quot; button on the campaigns page.</p>
                <div className="lobby-invite-code-row">
                  {invite?.code ? (
                    <>
                      <div className="lobby-invite-code">{invite.code}</div>
                      <button
                        type="button"
                        className={`lobby-copy-btn${copied ? ' copied' : ''}`}
                        onClick={handleCopyCode}
                      >
                        {copied ? <><i className="bi bi-check" aria-hidden="true" /> Copied!</> : <><i className="bi bi-copy" aria-hidden="true" /> Copy</>}
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="lobby-generate-btn"
                      onClick={handleGenerateCode}
                    >
                      <i className="bi bi-plus-circle" aria-hidden="true" /> Generate invite code
                    </button>
                  )}
                </div>
                {invite?.code && (
                  <p className="lobby-invite-hint">Code never expires. Share it anytime.</p>
                )}
              </div>
            </section>
          )}

          {/* Footer: begin */}
          <footer className="lobby-footer">
            <div className="lobby-locked-area">
              {canBegin ? (
                <button type="button" className="lobby-begin-btn" onClick={onBegin}>
                  <i className="bi bi-fire" aria-hidden="true" /> Begin adventure
                </button>
              ) : isOwner ? (
                <>
                  <button type="button" className="lobby-begin-btn lobby-begin-locked" disabled>
                    <i className="bi bi-lock" aria-hidden="true" /> Not ready to begin
                  </button>
                  <p className="lobby-locked-msg">Select a valid character and mark ready before starting.</p>
                </>
              ) : (
                <>
                  <button type="button" className="lobby-begin-btn lobby-begin-locked" disabled>
                    <i className="bi bi-lock" aria-hidden="true" /> Waiting for host
                  </button>
                  <p className="lobby-locked-msg">Only the campaign host can start the adventure.</p>
                </>
              )}
            </div>
          </footer>
        </div>
      </div>
    </div>
  )
}
