import { useState, useEffect } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { joinCampaign, apiFetch } from '../api/client'

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

export default function CampaignJoinPage() {
  const { id } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState(null)
  const [loading, setLoading] = useState(true)
  const [joining, setJoining] = useState(false)
  const [joined, setJoined] = useState(false)
  const [error, setError] = useState('')

  const code = searchParams.get('code') || ''

  useEffect(() => {
    const params = new URLSearchParams(searchParams)
    const codeParam = params.get('code') || ''
    const path = `/campaigns/${id}${codeParam ? `?code=${encodeURIComponent(codeParam)}` : ''}`

    apiFetch(path)
      .then((data) => setCampaign(data.campaign))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [id, searchParams])

  const handleJoin = async () => {
    if (!code.trim()) {
      setError('No invite code provided.')
      return
    }
    setJoining(true)
    setError('')
    try {
      await joinCampaign(id, code)
      setJoined(true)
      setTimeout(() => navigate(`/campaigns/${id}`), 1500)
    } catch (err) {
      setError(err.message)
    } finally {
      setJoining(false)
    }
  }

  if (loading) {
    return <div className="join-page"><div className="join-loading">Loading campaign...</div></div>
  }

  if (error && !campaign) {
    return <div className="join-page"><div className="join-error">{error}</div></div>
  }

  if (!campaign) {
    return <div className="join-page"><div className="join-error">Campaign not found.</div></div>
  }

  const gradient = getGradientSeed(campaign.name + (campaign.seed || ''))
  const initials = getInitials(campaign.name)

  return (
    <div className="join-page">
      {error && (
        <div className="join-error-banner" role="alert">
          <span>{error}</span>
          <button type="button" className="join-error-dismiss" onClick={() => setError('')} aria-label="Dismiss error">&times;</button>
        </div>
      )}

      <div className="join-container">
        <div className="join-card">
          <div className="join-hero">
            <div className="join-avatar" style={{ background: gradient }}>
              {initials}
            </div>
            <h1 className="join-title">{campaign.name}</h1>
            {campaign.description && (
              <p className="join-desc">{campaign.description}</p>
            )}
            <p className="join-owner">
              Campaign host: <strong>{campaign.owner_username || 'Unknown'}</strong>
            </p>
          </div>

          <div className="join-body">
            {joined ? (
              <div className="join-success">
                <div className="join-success-icon"><i className="bi bi-check-circle-fill"></i></div>
                <h2>Joined!</h2>
                <p>You've joined this campaign. Redirecting...</p>
              </div>
            ) : (
              <>
                <div className="join-input-group">
                  <label className="join-input-label" htmlFor="campaign-invite-code">Invite code</label>
                  <input
                    id="campaign-invite-code"
                    className="join-code-input"
                    type="text"
                    value={code}
                    onChange={(e) => {
                      const newCode = e.target.value.toUpperCase()
                      const params = new URLSearchParams(searchParams)
                      if (newCode) {
                        params.set('code', newCode)
                      } else {
                        params.delete('code')
                      }
                      navigate(`/join/${id}?${params.toString()}`, { replace: true })
                    }}
                    placeholder="Enter invite code..."
                    disabled={joining}
                    autoFocus={!code}
                  />
                </div>
                <button
                  className="join-btn"
                  onClick={handleJoin}
                  disabled={joining || !code.trim()}
                >
                  {joining ? (
                    'Joining...'
                  ) : (
                    <><i className="bi bi-box-arrow-in-right"></i> Join campaign</>
                  )}
                </button>
              </>
            )}
          </div>

          <div className="join-footer">
            <button className="join-back-btn" onClick={() => navigate('/')}>
              <i className="bi bi-arrow-left"></i> Back to campaigns
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
