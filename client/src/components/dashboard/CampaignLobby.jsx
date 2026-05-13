import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  listMembers,
  createInvite,
  getInvite,
  updateCampaign,
  removeMember,
} from '../../api/client'

function getInitials(name) {
  if (!name) return '?'
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function getGradientSeed(str) {
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

const DIFFICULTY_COLORS = {
  easy: '#4ade80',
  medium: '#facc15',
  hard: '#fb923c',
  deadly: '#f87171',
}

function getDifficultyColor(difficulty) {
  if (!difficulty) return null
  const key = difficulty.toLowerCase().trim()
  return DIFFICULTY_COLORS[key] || '#a78bfa'
}

export default function CampaignLobby({
  campaign,
  currentUser,
  onBegin,
}) {
  const navigate = useNavigate()
  const [members, setMembers] = useState([])
  const [invite, setInvite] = useState(null)
  const [requiredPlayers, setRequiredPlayers] = useState(
    () => (campaign.settings?.required_players) || 1
  )
  const [loading, setLoading] = useState(true)
  const [inviteLoading, setInviteLoading] = useState(false)
  const [copied, setCopied] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const isOwner = campaign.user_id === currentUser?.id

  const loadMembers = useCallback(async () => {
    try {
      const data = await listMembers(campaign.id)
      setMembers(data.members || [])
    } catch {
      // Silently keep current members on error
    } finally {
      setLoading(false)
    }
  }, [campaign.id])

  const checkExistingInvite = useCallback(async () => {
    try {
      const data = await getInvite(campaign.id)
      setInvite(data.invite)
    } catch {
      // Silently fail - we'll show the create button
    }
  }, [campaign.id])

  useEffect(() => {
    Promise.resolve().then(() => {
      loadMembers()
      checkExistingInvite()
    })
    const interval = setInterval(loadMembers, 5000)
    return () => clearInterval(interval)
  }, [loadMembers, checkExistingInvite])

  const handleCreateInvite = async () => {
    setInviteLoading(true)
    setError('')
    try {
      const data = await createInvite(campaign.id)
      setInvite(data.invite)
    } catch (err) {
      setError(err.message)
    } finally {
      setInviteLoading(false)
    }
  }

  const handleCopyInvite = () => {
    if (!invite) return
    const joinUrl = `${window.location.origin}/join/${campaign.id}?code=${invite.code}`
    const text = `Join my D&D campaign "${campaign.name}"!\nUse invite code: ${invite.code}\nOr click: ${joinUrl}`
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }).catch(() => {
      navigator.clipboard.writeText(invite.code).then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      }).catch(() => {})
    })
  }

  const handleUpdateRequired = async (newCount) => {
    const count = Math.max(members.length, Math.min(10, newCount))
    setRequiredPlayers(count)
    setSaving(true)
    try {
      const settings = { ...(campaign.settings || {}), required_players: count }
      await updateCampaign(campaign.id, { settings })
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const handleRemoveMember = async (userId) => {
    if (userId === currentUser?.id) return
    try {
      await removeMember(campaign.id, userId)
      loadMembers()
    } catch (err) {
      setError(err.message)
    }
  }

  if (loading) {
    return (
      <div className="lobby-page">
        <div className="lobby-loading">Loading party...</div>
      </div>
    )
  }

  const filledSlots = members.length
  const allSlotsFilled = filledSlots >= requiredPlayers
  const needed = requiredPlayers - filledSlots

  return (
    <div className="lobby-page">
      {error && (
        <div className="lobby-error" onClick={() => setError('')}>
          {error} <span className="lobby-error-dismiss">&times;</span>
        </div>
      )}

      <div className="lobby-container">
        <button className="lobby-back-btn" onClick={() => navigate('/')} title="Back to campaigns">
          <i className="bi bi-arrow-left"></i>
        </button>
        <div className="lobby-card">
          <div className="lobby-hero">
            <div
              className="lobby-hero-avatar"
              style={{ background: getGradientSeed(campaign.name + (campaign.seed || '')) }}
            >
              {getInitials(campaign.name)}
            </div>
            <h1 className="lobby-title">{campaign.name}</h1>
            <div className="lobby-hero-tags">
              {campaign.difficulty && (
                <span
                  className="lobby-badge"
                  style={{ '--badge-color': getDifficultyColor(campaign.difficulty) }}
                >
                  {campaign.difficulty}
                </span>
              )}
              {campaign.seed && (
                <span className="lobby-seed-tag">
                  <i className="bi bi-stars"></i> {campaign.seed}
                </span>
              )}
            </div>
            {campaign.description && (
              <p className="lobby-desc">{campaign.description}</p>
            )}
          </div>

          <div className="lobby-party-section">
            <div className="lobby-section-header">
              <div className="lobby-section-label">
                <i className="bi bi-people-fill"></i>
                <span>Your Party</span>
              </div>
              {isOwner && (
                <div className="lobby-size-control">
                  <button
                    className="lobby-size-btn"
                    onClick={() => handleUpdateRequired(requiredPlayers - 1)}
                    disabled={requiredPlayers <= Math.max(1, filledSlots) || saving}
                  >
                    <i className="bi bi-dash-lg"></i>
                  </button>
                  <span className="lobby-size-display">
                    {requiredPlayers} {requiredPlayers === 1 ? 'player' : 'players'}
                  </span>
                  <button
                    className="lobby-size-btn"
                    onClick={() => handleUpdateRequired(requiredPlayers + 1)}
                    disabled={requiredPlayers >= 10 || saving}
                  >
                    <i className="bi bi-plus-lg"></i>
                  </button>
                </div>
              )}
            </div>

            <div className="lobby-players-grid">
              {Array.from({ length: requiredPlayers }).map((_, i) => {
                const member = members[i]
                const isEmpty = !member

                if (isEmpty) {
                  return (
                    <div key={`empty-${i}`} className="lobby-player-slot lobby-slot-empty">
                      <div className="lobby-slot-circle lobby-slot-circle-empty">
                        <i className="bi bi-person-plus"></i>
                      </div>
                      <span className="lobby-slot-label">Open Slot</span>
                      <span className="lobby-slot-status">Waiting...</span>
                    </div>
                  )
                }

                const gradient = getGradientSeed(member.username || '')
                const initials = getInitials(member.username)
                const isDm = member.role === 'dm'
                const isSelf = member.user_id === currentUser?.id

                return (
                  <div key={member.user_id} className={`lobby-player-slot lobby-slot-filled ${isDm ? 'lobby-slot-dm' : ''}`}>
                    <div className="lobby-slot-circle" style={{ background: gradient }}>
                      {initials}
                      {isDm && <span className="lobby-slot-crown"><i className="bi bi-star-fill"></i></span>}
                    </div>
                    <span className="lobby-slot-name">
                      {member.username}
                      {isSelf && <span className="lobby-slot-you"> (you)</span>}
                    </span>
                    <span className={`lobby-slot-role ${isDm ? 'lobby-slot-role-dm' : ''}`}>
                      {isDm ? 'Dungeon Master' : 'Player'}
                    </span>
                    {isOwner && !isSelf && (
                      <button
                        className="lobby-slot-remove"
                        onClick={() => handleRemoveMember(member.user_id)}
                        title="Remove player"
                      >
                        <i className="bi bi-x-lg"></i>
                      </button>
                    )}
                  </div>
                )
              })}
            </div>

            {!allSlotsFilled && (
              <div className="lobby-slots-status">
                <div className="lobby-slots-bar">
                  <div
                    className="lobby-slots-fill"
                    style={{
                      width: `${(filledSlots / requiredPlayers) * 100}%`,
                    }}
                  />
                </div>
                <span className="lobby-slots-text">
                  {filledSlots} of {requiredPlayers} players &middot; {needed} more needed
                </span>
              </div>
            )}
          </div>

          {isOwner && (
            <div className="lobby-invite-section">
              <div className="lobby-section-label">
                <i className="bi bi-link-45deg"></i>
                <span>Invite Players</span>
              </div>
              <div className="lobby-invite-card">
                <p className="lobby-invite-desc">
                  Share this invite code with your friends so they can join the campaign.
                </p>
                {invite ? (
                  <div className="lobby-invite-code-area">
                    <div className="lobby-invite-code-row">
                      <code className="lobby-invite-code">{invite.code}</code>
                      <button
                        className="lobby-copy-btn"
                        onClick={handleCopyInvite}
                      >
                        {copied ? (
                          <><i className="bi bi-check-lg"></i> Copied</>
                        ) : (
                          <><i className="bi bi-clipboard"></i> Copy</>
                        )}
                      </button>
                    </div>
                    <p className="lobby-invite-hint">
                      <i className="bi bi-info-circle"></i> This code stays active for this campaign
                    </p>
                  </div>
                ) : (
                  <button
                    className="lobby-generate-btn"
                    onClick={handleCreateInvite}
                    disabled={inviteLoading}
                  >
                    {inviteLoading ? (
                      'Generating...'
                    ) : (
                      <><i className="bi bi-link-45deg"></i> Generate Invite Code</>
                    )}
                  </button>
                )}
              </div>
            </div>
          )}

          <div className="lobby-footer">
            {allSlotsFilled ? (
              <button className="lobby-begin-btn" onClick={onBegin}>
                <i className="bi bi-play-fill"></i> Begin Adventure
              </button>
            ) : (
              <div className="lobby-locked-area">
                <button className="lobby-begin-btn lobby-begin-locked" disabled>
                  <i className="bi bi-lock-fill"></i> Begin Adventure
                </button>
                <p className="lobby-locked-msg">
                  {isOwner
                    ? `Wait for ${needed} more player${needed !== 1 ? 's' : ''} to join before starting your adventure.`
                    : `Waiting for the party to fill up — ${needed} more player${needed !== 1 ? 's' : ''} needed.`}
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
