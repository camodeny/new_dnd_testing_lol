'use client'

import { useState, useCallback, useEffect } from 'react'
import { campaigns as campaignsApi, campaignMembers as membersApi, characters as charactersApi } from '@/lib/api'
import type { Campaign, CampaignInvite, CampaignMember, Character, LobbyEligibility, User } from '@/types'

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
  const [invites, setInvites] = useState<CampaignInvite[]>([])
  const [outstanding, setOutstanding] = useState(0)
  const [copiedCode, setCopiedCode] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteLabel, setInviteLabel] = useState('')
  const [inviteBusy, setInviteBusy] = useState(false)
  const [inviteError, setInviteError] = useState('')
  const [inviteNotice, setInviteNotice] = useState('')
  // Render-safe origin for shareable links (#242 review): populated after
  // mount so server rendering/prerendering never touches `window`.
  const [origin, setOrigin] = useState('')
  useEffect(() => {
    setOrigin(window.location.origin)
  }, [])
  const [owned, setOwned] = useState<Character[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [busy, setBusy] = useState(false)
  const [lobbyError, setLobbyError] = useState('')

  const refreshLobby = useCallback(async () => {
    try {
      const data = await membersApi.getLobby(campaign.id)
      setMembers(data.members ?? [])
      setEligibility(data.eligibility ?? null)
      setRevision((current) => data.campaign?.revision ?? current)
      setLaunchLocked(Boolean(data.launch_locked))
      // Joined vs outstanding invited state (#242). Non-owners receive
      // masked email hints only — never raw addresses.
      if (Array.isArray((data as { invites?: CampaignInvite[] }).invites)) {
        setInvites((data as { invites?: CampaignInvite[] }).invites ?? [])
      }
      if (typeof (data as { outstanding_invites?: number }).outstanding_invites === 'number') {
        setOutstanding((data as { outstanding_invites?: number }).outstanding_invites ?? 0)
      }
    } catch {
      setEligibility(null)
      // Fall back to members-only projection if lobby endpoint is unavailable
      try {
        const data = await membersApi.listMembers(campaign.id)
        setMembers((data as { members?: CampaignMember[] }).members ?? [])
      } catch { /* no-op */ }
    }
  }, [campaign.id])

  useEffect(() => {
    void refreshLobby()
    const timer = window.setInterval(() => void refreshLobby(), 2_000)
    return () => window.clearInterval(timer)
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
      .listInvites(campaign.id)
      .then((data) => setInvites((data as { invites?: CampaignInvite[] }).invites ?? []))
      .catch(() => {})
  }, [campaign.id, isOwner])

  const refreshInvites = useCallback(async () => {
    if (!isOwner) return
    try {
      const data = await membersApi.listInvites(campaign.id)
      setInvites((data as { invites?: CampaignInvite[] }).invites ?? [])
    } catch { /* no-op */ }
  }, [campaign.id, isOwner])

  const handleCreateInvite = useCallback(async () => {
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    try {
      await membersApi.createInvite(campaign.id, {
        intended_email: inviteEmail.trim() || undefined,
        recipient_label: inviteLabel.trim() || undefined,
      })
      setInviteEmail('')
      setInviteLabel('')
      await Promise.all([refreshInvites(), refreshLobby()])
      setInviteNotice('Invite created — share the link below.')
    } catch (err) {
      setInviteError((err as Error).message)
    } finally {
      setInviteBusy(false)
    }
  }, [campaign.id, inviteEmail, inviteLabel, refreshInvites, refreshLobby])

  const handleRevokeInvite = useCallback(async (code: string) => {
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    try {
      await membersApi.revokeInvite(campaign.id, code, revision, newKey())
      await Promise.all([refreshInvites(), refreshLobby()])
      setInviteNotice(`Invite ${code} revoked.`)
    } catch (err) {
      await refreshLobby()
      setInviteError((err as Error).message)
    } finally {
      setInviteBusy(false)
    }
  }, [campaign.id, revision, refreshInvites, refreshLobby])

  const handleSendEmail = useCallback(async (code: string, email?: string | null) => {
    const target = (email ?? '').trim()
    if (!target) {
      setInviteError('That invite has no email address — add one when creating the invite.')
      return
    }
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    try {
      const result = await membersApi.sendInviteEmail(campaign.id, code, target) as {
        ok?: boolean
        delivery?: { sent?: boolean; error?: string | null }
      }
      await Promise.all([refreshInvites(), refreshLobby()])
      if (result?.delivery?.sent) {
        setInviteNotice(`Invite email sent to ${target}.`)
      } else {
        // Email failure never invalidates the link/code (#242): the invite
        // stays usable and the owner can copy the link or retry sending.
        setInviteNotice(`Email not delivered (${result?.delivery?.error ?? 'provider unavailable'}) — the link below still works; retry anytime.`)
      }
    } catch (err) {
      setInviteError((err as Error).message)
    } finally {
      setInviteBusy(false)
    }
  }, [campaign.id, refreshInvites, refreshLobby])

  const handleCopy = useCallback(async (code: string, path?: string | null) => {
    const text = origin ? `${origin}/invite/${code}` : (path ?? `/invite/${code}`)
    await navigator.clipboard.writeText(text).catch(() => {})
    setCopiedCode(code)
    setTimeout(() => setCopiedCode((current) => (current === code ? null : current)), 2000)
  }, [origin])

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
      await refreshLobby()
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
      await refreshLobby()
      setLobbyError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [busy, campaign.id, revision, refreshLobby])

  const handleBegin = useCallback(async () => {
    if (busy || launchLocked || !isOwner || !eligibility?.eligible) return
    setBusy(true)
    setLobbyError('')
    try {
      const result = await campaignsApi.transitionLifecycle(campaign.id, revision, 'starting', newKey())
      setRevision(result.campaign.revision)
      setLaunchLocked(true)
      onBegin()
    } catch (err) {
      await refreshLobby()
      setLobbyError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }, [busy, launchLocked, isOwner, eligibility, campaign.id, revision, onBegin, refreshLobby])

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

          {/* Invite section — issue #242 */}
          <section className="lobby-invite-section">
            <div className="lobby-section-header">
              <span className="lobby-section-label">
                <i className="bi bi-link-45deg" aria-hidden="true" /> Invite players
              </span>
              {outstanding > 0 && (
                <span style={{ fontSize: '0.72rem', color: 'var(--ink-muted)' }}>
                  {outstanding} outstanding
                </span>
              )}
            </div>
            <div className="lobby-invite-card">
              <p className="lobby-invite-desc">Share a link — it survives sign-up: new players land straight in this lobby after creating their account.</p>
              {invites.filter((inv) => inv.usable !== false && inv.status === 'active').map((inv) => {
                const key = inv.id ?? inv.code ?? inv.intended_email_hint ?? 'invite'
                // Bearer codes/links are owner-only (#242 review): members
                // see who is outstanding, never the credential itself.
                if (!isOwner || !inv.code) {
                  const who = inv.recipient_label ?? inv.intended_email_hint ?? 'Invited player'
                  return (
                    <div key={key} className="lobby-invite-code-row" style={{ marginBottom: 8 }}>
                      <span style={{ alignSelf: 'center', fontSize: '0.78rem', color: 'var(--ink-muted)' }}>
                        <i className="bi bi-envelope" aria-hidden="true" /> {who} · invited
                      </span>
                    </div>
                  )
                }
                const link = origin ? `${origin}/invite/${inv.code}` : (inv.invite_url_path ?? `/invite/${inv.code}`)
                return (
                <div key={key} className="lobby-invite-code-row" style={{ marginBottom: 8 }}>
                  <div className="lobby-invite-code" title={inv.recipient_label ?? inv.intended_email ?? inv.code}>
                    {link}
                  </div>
                  <button
                    type="button"
                    className={`lobby-copy-btn${copiedCode === inv.code ? ' copied' : ''}`}
                    onClick={() => void handleCopy(inv.code as string, inv.invite_url_path)}
                  >
                    {copiedCode === inv.code ? <><i className="bi bi-check" aria-hidden="true" /> Copied!</> : <><i className="bi bi-copy" aria-hidden="true" /> Copy link</>}
                  </button>
                  {isOwner && (
                    <>
                      {inv.intended_email && (
                        <span style={{ alignSelf: 'center', fontSize: '0.72rem', color: 'var(--ink-muted)' }}>
                          {inv.intended_email}
                          {inv.recipient_label && ` · ${inv.recipient_label}`}
                        </span>
                      )}
                      {inv.intended_email && (
                        <button
                          type="button"
                          className="lobby-generate-btn"
                          disabled={inviteBusy}
                          onClick={() => void handleSendEmail(inv.code as string, inv.intended_email)}
                          title={inv.last_delivery_status === 'sent' ? 'Resend email' : 'Send email'}
                        >
                          <i className="bi bi-envelope" aria-hidden="true" /> {inv.last_delivery_status === 'sent' ? 'Resend' : 'Email'}
                        </button>
                      )}
                      <button
                        type="button"
                        className="lobby-generate-btn"
                        disabled={inviteBusy}
                        onClick={() => void handleRevokeInvite(inv.code as string)}
                      >
                        Revoke
                      </button>
                    </>
                  )}
                </div>
                )
              })}
              {isOwner && (
                <>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
                    <input
                      type="email"
                      aria-label="Invitee email (optional)"
                      placeholder="friend@example.com (optional)"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      disabled={inviteBusy}
                      style={{ flex: '2 1 200px', padding: '8px 10px', borderRadius: 8 }}
                    />
                    <input
                      type="text"
                      aria-label="Recipient label (optional)"
                      placeholder="Name or note (optional)"
                      value={inviteLabel}
                      onChange={(e) => setInviteLabel(e.target.value)}
                      disabled={inviteBusy}
                      style={{ flex: '1 1 140px', padding: '8px 10px', borderRadius: 8 }}
                    />
                    <button
                      type="button"
                      className="lobby-generate-btn"
                      onClick={() => void handleCreateInvite()}
                      disabled={inviteBusy}
                    >
                      <i className="bi bi-plus-circle" aria-hidden="true" /> New invite link
                    </button>
                  </div>
                  {inviteError && <p className="lobby-invite-hint" role="alert">{inviteError}</p>}
                  {inviteNotice && <p className="lobby-invite-hint" role="status">{inviteNotice}</p>}
                  {invites.length === 0 && (
                    <p className="lobby-invite-hint">No invites yet — create one above.</p>
                  )}
                </>
              )}
              {!isOwner && invites.length === 0 && (
                <p className="lobby-invite-hint">No outstanding invites.</p>
              )}
            </div>
          </section>

          {/* Footer: begin */}
          <footer className="lobby-footer">
            <div className="lobby-locked-area">
              {canBegin ? (
                <button type="button" className="lobby-begin-btn" onClick={() => void handleBegin()} disabled={busy || launchLocked}>
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
