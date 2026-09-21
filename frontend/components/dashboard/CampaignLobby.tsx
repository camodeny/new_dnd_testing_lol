'use client'

import { useState, useCallback, useEffect } from 'react'
import Modal from '@/components/common/Modal'
import { campaigns as campaignsApi, campaignMembers as membersApi, characters as charactersApi } from '@/lib/api'
import type { Campaign, CampaignInvite, CampaignMember, Character, LobbyEligibility, PartyAdvice, PartyComposition, User } from '@/types'

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

async function tryCopyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return false
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

// Owner invite lists arrive oldest-first (CampaignInvite.created_at.asc()),
// so the newest usable code is the max by created_at with later positions
// winning ties/missing timestamps.
function newestUsableInvite(list: CampaignInvite[]): CampaignInvite | undefined {
  const usable = list.filter((inv) => inv.usable !== false && inv.status === 'active' && inv.code)
  if (usable.length === 0) return undefined
  return usable.reduce((latest, inv) => ((inv.created_at ?? '') >= (latest.created_at ?? '') ? inv : latest))
}

export default function CampaignLobby({ campaign, currentUser, isOwner, onBegin }: CampaignLobbyProps) {
  const [members, setMembers] = useState<CampaignMember[]>([])
  const [eligibility, setEligibility] = useState<LobbyEligibility | null>(null)
  const [revision, setRevision] = useState<number>(campaign.revision)
  const [launchLocked, setLaunchLocked] = useState(false)
  const [invites, setInvites] = useState<CampaignInvite[]>([])
  const [linkCopied, setLinkCopied] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteBusy, setInviteBusy] = useState(false)
  const [inviteError, setInviteError] = useState('')
  const [inviteNotice, setInviteNotice] = useState('')
  // Owner-only fallback link shown as selectable text when the clipboard
  // write fails (the copy-only UI otherwise leaves no way to share it).
  const [manualLink, setManualLink] = useState<string | null>(null)
  // Render-safe origin for shareable links (#242 review): populated after
  // mount so server rendering/prerendering never touches `window`.
  const [origin, setOrigin] = useState('')
  useEffect(() => {
    setOrigin(window.location.origin)
  }, [])
  const [owned, setOwned] = useState<Character[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [charModalOpen, setCharModalOpen] = useState(false)
  // Public party composition + advisory gaps (#244). Composition comes from
  // the lobby payload; advice is fetched on demand for the creator.
  const [composition, setComposition] = useState<PartyComposition | null>(null)
  const [advice, setAdvice] = useState<PartyAdvice | null>(null)
  // Private setup lore (#244): the player's own secret notes for the DM.
  // Never rendered for other players; the owner sees nothing here.
  const [lore, setLore] = useState('')
  const [loreSaved, setLoreSaved] = useState(false)
  const [loreBusy, setLoreBusy] = useState(false)
  const [loreError, setLoreError] = useState('')
  const [invitesModalOpen, setInvitesModalOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [lobbyError, setLobbyError] = useState('')

  const refreshLobby = useCallback(async () => {
    try {
      const data = await membersApi.getLobby(campaign.id)
      setMembers(data.members ?? [])
      setEligibility(data.eligibility ?? null)
      setRevision((current) => data.campaign?.revision ?? current)
      setLaunchLocked(Boolean(data.launch_locked))
      setComposition((data as { party_composition?: PartyComposition }).party_composition ?? null)
      // Joined vs outstanding invited state (#242). Non-owners receive
      // masked email hints only — never raw addresses.
      if (Array.isArray((data as { invites?: CampaignInvite[] }).invites)) {
        setInvites((data as { invites?: CampaignInvite[] }).invites ?? [])
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

  const handleSendInvite = useCallback(async () => {
    const email = inviteEmail.trim()
    if (!email || inviteBusy) return
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    setManualLink(null)
    try {
      const created = await membersApi.createInvite(campaign.id, { intended_email: email })
      const result = await membersApi.sendInviteEmail(campaign.id, created.code, email) as {
        delivery?: { sent?: boolean; error?: string | null }
      }
      setInviteEmail('')
      await Promise.all([refreshInvites(), refreshLobby()])
      if (result?.delivery?.sent) {
        setInviteNotice(`Invite sent to ${email}.`)
      } else {
        // Email failure never invalidates the invite: try to copy the link
        // so it can be shared manually; on clipboard failure surface the
        // owner-only link as selectable text instead of claiming a copy.
        const link = origin ? `${origin}/invite/${created.code}` : `/invite/${created.code}`
        const copied = await tryCopyText(link)
        if (copied) {
          setInviteNotice(`Couldn't email ${email} (${result?.delivery?.error ?? 'provider unavailable'}) — invite link copied, share it manually.`)
        } else {
          setManualLink(link)
          setInviteNotice(`Couldn't email ${email} (${result?.delivery?.error ?? 'provider unavailable'}) — copy the link below manually.`)
        }
      }
    } catch (err) {
      setInviteError((err as Error).message)
    } finally {
      setInviteBusy(false)
    }
  }, [campaign.id, inviteEmail, inviteBusy, origin, refreshInvites, refreshLobby])

  const handleRevokeInvite = useCallback(async (code: string) => {
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    setManualLink(null)
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

  const handleShareLink = useCallback(async () => {
    if (inviteBusy) return
    setInviteBusy(true)
    setInviteError('')
    setInviteNotice('')
    setManualLink(null)
    try {
      // Reuse the latest active link; only mint a new code when none exists.
      const usable = newestUsableInvite(invites)
      const code = usable?.code ?? (await membersApi.createInvite(campaign.id)).code
      const link = origin ? `${origin}/invite/${code}` : `/invite/${code}`
      const copied = await tryCopyText(link)
      if (!usable) await Promise.all([refreshInvites(), refreshLobby()])
      if (copied) {
        setLinkCopied(true)
        setTimeout(() => setLinkCopied(false), 2000)
      } else {
        // Clipboard unavailable/denied: surface the owner-only link as
        // selectable text instead of reporting a copy that never happened.
        setManualLink(link)
        setInviteError(`Couldn't copy the invite link — copy it below manually: ${link}`)
      }
    } catch (err) {
      setInviteError((err as Error).message)
    } finally {
      setInviteBusy(false)
    }
  }, [campaign.id, invites, inviteBusy, origin, refreshInvites, refreshLobby])

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

  // Private setup lore (#244): load own lore when the selected character
  // changes; save/delete are lobby-only and fail closed for others.
  useEffect(() => {
    if (!myCharId) { setLore(''); setLoreSaved(false); return }
    membersApi.getCharacterLore(campaign.id, myCharId)
      .then((data) => { setLore((data.lore as { content?: string }).content ?? ''); setLoreSaved(true) })
      .catch(() => { setLore(''); setLoreSaved(false) })
  }, [campaign.id, myCharId])

  const handleSaveLore = useCallback(async () => {
    if (!myCharId || loreBusy || launchLocked) return
    setLoreBusy(true)
    setLoreError('')
    try {
      await membersApi.putCharacterLore(campaign.id, myCharId, revision, lore, newKey())
      setLoreSaved(true)
      await refreshLobby()
    } catch (err) {
      await refreshLobby()
      setLoreError((err as Error).message)
    } finally {
      setLoreBusy(false)
    }
  }, [myCharId, loreBusy, launchLocked, campaign.id, revision, lore, refreshLobby])

  const handleDeleteLore = useCallback(async () => {
    if (!myCharId || loreBusy || launchLocked) return
    setLoreBusy(true)
    setLoreError('')
    try {
      await membersApi.deleteCharacterLore(campaign.id, myCharId, revision, newKey())
      setLore('')
      setLoreSaved(false)
      await refreshLobby()
    } catch (err) {
      await refreshLobby()
      setLoreError((err as Error).message)
    } finally {
      setLoreBusy(false)
    }
  }, [myCharId, loreBusy, launchLocked, campaign.id, revision, refreshLobby])

  const handleLoadAdvice = useCallback(async () => {
    try {
      const data = await membersApi.getPartyAdvice(campaign.id)
      setAdvice(data.advice)
    } catch { /* advisory only */ }
  }, [campaign.id])

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
  const activeInvites = invites.filter((inv) => inv.usable !== false && inv.status === 'active')
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

          {/* Right: party section */}
          <section className="lobby-party-section">
            <div className="lobby-section-header">
              <span className="lobby-section-label">
                <i className="bi bi-people-fill" aria-hidden="true" /> The Party
              </span>
            </div>

            <div className="lobby-players-grid">
              {members.map((member) => {
                const charId = member.selected_character_id ?? member.character_id ?? null
                const isMine = member.user_id === currentUser?.id
                // Your own seat opens the character picker — other seats are display-only.
                const SlotTag = (isMine && !launchLocked ? 'button' : 'div') as 'button' | 'div'
                return (
                  <SlotTag
                    key={member.user_id}
                    className={`lobby-player-slot lobby-slot-filled${isMine && !launchLocked ? ' lobby-slot-mine' : ''}`}
                    {...(SlotTag === 'button'
                      ? {
                        type: 'button',
                        onClick: () => { setLobbyError(''); setCharModalOpen(true) },
                        'aria-label': 'Choose your character',
                      }
                      : {})}
                  >
                    <div className="lobby-slot-circle">
                      {member.username?.slice(0, 2).toUpperCase() ?? '?'}
                    </div>
                    <div className={`lobby-slot-name${isMine ? ' lobby-slot-you' : ''}`}>
                      {member.username}
                      {isMine && ' (you)'}
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
                  </SlotTag>
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
            {/* Public party composition (#244): class/role coverage only. */}
            {composition && (
              <div style={{ marginTop: 12, fontSize: '0.78rem', color: 'var(--ink-faint)' }}>
                <span>Party: {composition.ready_count}/{composition.size} ready</span>
                {Object.entries(composition.class_counts ?? {}).map(([cls, n]) => (
                  <span key={cls} style={{ marginLeft: 8 }}>{cls} × {n}</span>
                ))}
                <button type="button" onClick={() => void handleLoadAdvice()} style={{ marginLeft: 12 }} aria-label="Get party advice">
                  Party advice
                </button>
              </div>
            )}
            {advice && (
              <ul style={{ marginTop: 8, fontSize: '0.78rem' }}>
                {advice.suggestions.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            )}
          </section>

          {/* Private character lore (#244): your secrets for the DM only. */}
          {myCharId && !launchLocked && (
            <section className="lobby-lore-section" aria-label="Private character lore">
              <div className="lobby-section-header">
                <span className="lobby-section-label">
                  <i className="bi bi-incognito" aria-hidden="true" /> Private lore (only you + DM)
                </span>
              </div>
              <textarea
                value={lore}
                onChange={(e) => setLore(e.target.value)}
                placeholder="Secrets, backstory hooks, personal goals — hidden from the party and the host"
                rows={4}
                maxLength={4000}
                style={{ width: '100%' }}
                aria-label="Private character lore"
              />
              {loreError && <div style={{ color: 'var(--danger)', fontSize: '0.75rem' }}>{loreError}</div>}
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                <button type="button" onClick={() => void handleSaveLore()} disabled={loreBusy} aria-label="Save private lore">
                  {loreSaved ? 'Update private lore' : 'Save private lore'}
                </button>
                {loreSaved && (
                  <button type="button" onClick={() => void handleDeleteLore()} disabled={loreBusy} aria-label="Delete private lore">
                    Delete
                  </button>
                )}
              </div>
            </section>
          )}

          {/* Invite section — issue #242 */}
          <section className="lobby-invite-section">
            <div className="lobby-invite-card">
              <div className="lobby-section-header" style={{ marginBottom: 16 }}>
                <span className="lobby-section-label">
                  <i className="bi bi-link-45deg" aria-hidden="true" /> Invite players
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  {activeInvites.length > 0 && (
                    <button
                      type="button"
                      className="lobby-generate-btn"
                      onClick={() => setInvitesModalOpen(true)}
                      title="See who's been invited"
                      aria-label="See who's been invited"
                      style={{ minHeight: 32, padding: '0 10px' }}
                    >
                      <i className="bi bi-people" aria-hidden="true" />
                    </button>
                  )}
                  {isOwner && (
                    <button
                      type="button"
                      className="lobby-generate-btn"
                      onClick={() => void handleShareLink()}
                      disabled={inviteBusy}
                      title={linkCopied ? 'Copied!' : 'Copy a shareable invite link'}
                      aria-label="Copy a shareable invite link"
                      style={{ minHeight: 32, padding: '0 10px' }}
                    >
                      {linkCopied
                        ? <><i className="bi bi-check" aria-hidden="true" /> Copied</>
                        : <i className="bi bi-link-45deg" aria-hidden="true" />}
                    </button>
                  )}
                </div>
              </div>
              {isOwner ? (
                <>
                  <p className="lobby-invite-desc">Enter their email — we&apos;ll send an invite to join this lobby.</p>
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <input
                      type="email"
                      aria-label="Invitee email"
                      placeholder="friend@example.com"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') void handleSendInvite() }}
                      disabled={inviteBusy}
                      style={{ flex: '1 1 auto', minWidth: 0, padding: '8px 10px', borderRadius: 8 }}
                    />
                    <button
                      type="button"
                      className="lobby-generate-btn"
                      onClick={() => void handleSendInvite()}
                      disabled={inviteBusy || !inviteEmail.trim()}
                    >
                      <i className="bi bi-send" aria-hidden="true" /> Send invite
                    </button>
                  </div>
                  {inviteError && <p className="lobby-invite-hint" role="alert">{inviteError}</p>}
                  {inviteNotice && <p className="lobby-invite-hint" role="status">{inviteNotice}</p>}
                  {manualLink && (
                    <p className="lobby-invite-hint" role="status" style={{ wordBreak: 'break-all' }}>
                      Copy this link manually: <code>{manualLink}</code>
                    </p>
                  )}
                </>
              ) : (
                activeInvites.length === 0 && (
                  <p className="lobby-invite-hint">No outstanding invites.</p>
                )
              )}
            </div>
          </section>

          {/* Footer: begin */}
          <footer className="lobby-footer">
            <div className="lobby-locked-area">
              {canBegin ? (
                <>
                  <button type="button" className="lobby-begin-btn" onClick={() => void handleBegin()} disabled={busy || launchLocked}>
                    <i className="bi bi-fire" aria-hidden="true" /> Begin adventure
                  </button>
                  {lobbyError && (
                    <p className="lobby-locked-msg" role="alert">{lobbyError}</p>
                  )}
                </>
              ) : isOwner ? (
                <>
                  <button type="button" className="lobby-begin-btn lobby-begin-locked" disabled>
                    <i className="bi bi-lock" aria-hidden="true" /> Not ready to begin
                  </button>
                  {lobbyError && (
                    <p className="lobby-locked-msg" role="alert">{lobbyError}</p>
                  )}
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
        <Modal open={charModalOpen} onClose={() => setCharModalOpen(false)} title="Your character" titleId="lobby-character-title">
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
            <a className="lobby-copy-btn" href={`/characters/new?campaign=${encodeURIComponent(campaign.id)}`}>
              New with party advice
            </a>
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
        </Modal>
        <Modal open={invitesModalOpen} onClose={() => setInvitesModalOpen(false)} title="Invited players" titleId="lobby-invited-title">
          {activeInvites.map((inv) => {
            const key = inv.id ?? inv.code ?? inv.intended_email_hint ?? 'invite'
            // Bearer codes/links are owner-only (#242 review): members
            // see who is outstanding, never the credential itself.
            const who = isOwner
              ? (inv.intended_email ?? inv.recipient_label ?? inv.intended_email_hint ?? 'Shareable link')
              : (inv.recipient_label ?? inv.intended_email_hint ?? 'Invited player')
            return (
              <div key={key} className="lobby-invite-code-row" style={{ marginBottom: 8 }}>
                <span style={{ alignSelf: 'center', minWidth: 0, flex: '1 1 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '0.78rem', color: 'var(--ink-muted)' }} title={who}>
                  <i className="bi bi-envelope" aria-hidden="true" /> {who} · invited
                </span>
                {isOwner && inv.code && (
                  <button
                    type="button"
                    className="lobby-generate-btn"
                    disabled={inviteBusy}
                    onClick={() => void handleRevokeInvite(inv.code as string)}
                  >
                    Revoke
                  </button>
                )}
              </div>
            )
          })}
          {activeInvites.length === 0 && (
            <p className="lobby-invite-hint">No outstanding invites.</p>
          )}
        </Modal>
      </div>
    </div>
  )
}
