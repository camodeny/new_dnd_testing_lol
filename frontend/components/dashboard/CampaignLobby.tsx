'use client'

import { useState, useCallback, useEffect } from 'react'
import { campaignMembers as membersApi } from '@/lib/api'
import type { Campaign, CampaignMember, User } from '@/types'

interface CampaignLobbyProps {
  campaign: Campaign
  currentUser: User | null
  isOwner: boolean
  onBegin: () => void
}

export default function CampaignLobby({ campaign, currentUser, isOwner, onBegin }: CampaignLobbyProps) {
  const [members, setMembers] = useState<CampaignMember[]>([])
  const [invite, setInvite] = useState<{ code?: string } | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    membersApi
      .listMembers(campaign.id)
      .then((data) => setMembers((data as { members?: CampaignMember[] }).members ?? []))
      .catch(() => {})
  }, [campaign.id])

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

  const initials = campaign.name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()

  const filledSlots = members.length
  const totalSlots = (campaign as { required_players?: number }).required_players ?? members.length
  const canBegin = isOwner

  return (
    <div className="lobby-page">
      <div className="lobby-container">
        <div className="lobby-card">
          {/* Left: hero panel */}
          <div className="lobby-hero">
            <div className="lobby-hero-avatar" aria-hidden="true">{initials}</div>
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
              <div className="lobby-size-control">
                <div className="lobby-size-display">
                  {filledSlots} / {totalSlots} player{totalSlots !== 1 ? 's' : ''}
                </div>
              </div>
            </div>

            <div className="lobby-players-grid">
              {members.map((member) => (
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
                  {member.role === 'owner' && (
                    <span style={{ position: 'absolute', top: 6, right: 6, fontSize: '0.55rem', fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', padding: '3px 6px', borderRadius: 4, background: 'var(--ember-soft)', color: 'var(--ember-hover)' }}>Host</span>
                  )}
                </div>
              ))}

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
