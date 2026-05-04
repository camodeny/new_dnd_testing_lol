import { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getCampaign, updateCampaign, getCampaignCharacters,
  startSession, endSession, getSession, sendMessage,
  getCharacters,
  addCampaignCharacter,
  listMembers,
  getCampaignPlanning,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import PartyRoster from '../components/dashboard/PartyRoster'
import SessionPanel from '../components/dashboard/SessionPanel'
import CampaignLobby from '../components/dashboard/CampaignLobby'
import CharacterPlanningMode from '../components/dashboard/CharacterPlanningMode'

function formatDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function getInitials(name) {
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

const STATUS_COLORS = {
  active: '#4ade80',
  paused: '#facc15',
  completed: '#6b7280',
  archived: '#6b7280',
}

function getClassSummary(character) {
  if (character.classes?.length) {
    return character.classes.map((c) => `${c.class_name} ${c.level}`).join(', ')
  }
  return `Level ${character.total_level ?? '?'}`
}

async function fetchCampaignPageData(id) {
  const [campData, charData] = await Promise.all([
    getCampaign(id),
    getCampaignCharacters(id),
  ])
  const campaign = campData.campaign
  const activeSession = campaign.active_session || null
  const messages = activeSession
    ? await getSession(activeSession.id)
      .then((data) => data.session?.messages || [])
      .catch(() => [])
    : []

  return {
    campaign,
    characters: charData.characters || [],
    activeSession,
    messages,
  }
}

export default function CampaignViewPage({ user }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const [campaign, setCampaign] = useState(null)
  const [characters, setCharacters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [session, setSession] = useState(null)
  const [messages, setMessages] = useState([])
  const [showSettings, setShowSettings] = useState(false)
  const [campaignName, setCampaignName] = useState('')
  const [campaignDesc, setCampaignDesc] = useState('')
  const [showLobby, setShowLobby] = useState(false)
  const [showPlanning, setShowPlanning] = useState(false)

  const [showImport, setShowImport] = useState(false)
  const [availableChars, setAvailableChars] = useState([])
  const [importLoading, setImportLoading] = useState(false)
  const [aiThinking, setAiThinking] = useState(false)

  const loadData = useCallback(async () => {
    try {
      const data = await fetchCampaignPageData(id)
      setCampaign(data.campaign)
      setCharacters(data.characters)
      setSession(data.activeSession)
      setMessages(data.messages)
      const required = data.campaign.settings?.required_players || 1
      const memData = await listMembers(id)
      const memberCount = (memData.members || []).length
      if (!data.activeSession && memberCount < required) {
        setShowLobby(true)
        setShowPlanning(false)
      } else if (!data.activeSession) {
        const planningData = await getCampaignPlanning(id)
        setShowLobby(false)
        setShowPlanning(!planningData.planning?.all_ready)
      } else {
        setShowLobby(false)
        setShowPlanning(false)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    let isMounted = true

    async function loadAll() {
      try {
        const data = await fetchCampaignPageData(id)
        if (!isMounted) return

        setCampaign(data.campaign)
        setCharacters(data.characters)
        setSession(data.activeSession)
        setMessages(data.messages)

        const required = data.campaign.settings?.required_players || 1
        try {
          const memData = await listMembers(id)
          if (!isMounted) return
          const memberCount = (memData.members || []).length
          if (!data.activeSession && memberCount < required) {
            setShowLobby(true)
            setShowPlanning(false)
          } else if (!data.activeSession) {
            const planningData = await getCampaignPlanning(id)
            if (!isMounted) return
            setShowLobby(false)
            setShowPlanning(!planningData.planning?.all_ready)
          } else {
            setShowLobby(false)
            setShowPlanning(false)
          }
        } catch {
          // If planning fetch fails, still show dashboard with the start-session backend gate.
          if (isMounted) {
            setShowLobby(false)
            setShowPlanning(false)
          }
        }
      } catch (err) {
        if (isMounted) setError(err.message)
      } finally {
        if (isMounted) setLoading(false)
      }
    }

    loadAll()

    return () => {
      isMounted = false
    }
  }, [id])

  const handleStartSession = async () => {
    try {
      const data = await startSession(id)
      const newSession = data.session
      setSession(newSession)
      setMessages([])
      return newSession
    } catch (err) {
      setError(err.message)
    }
  }

  const handleEndSession = async () => {
    if (!session) return
    try {
      await endSession(session.id, '')
      setSession(null)
      setMessages([])
      loadData()
    } catch (err) {
      setError(err.message)
    }
  }

  const handleSendMessage = async (content) => {
    if (!session) return
    setAiThinking(true)
    try {
      const data = await sendMessage(session.id, content)
      setMessages((prev) => [...prev, ...data.messages])
    } catch (err) {
      setError(err.message)
    } finally {
      setAiThinking(false)
    }
  }

  const handleRollDice = (sides, rolls, total, modifier) => {
    if (!session) return
    const rollMsg = `🎲 d${sides} = ${rolls.join(', ')}${modifier ? ` + ${modifier} = ${total}` : ` → ${total}`}`
    handleSendMessage(rollMsg)
  }

  const handleUpdateSettings = async () => {
    try {
      const data = await updateCampaign(id, { name: campaignName, description: campaignDesc })
      setCampaign(data.campaign)
      setShowSettings(false)
    } catch (err) {
      setError(err.message)
    }
  }

  const openImport = async () => {
    setShowImport(true)
    setImportLoading(true)
    setError('')
    try {
      const data = await getCharacters()
      const unassigned = (data.characters || []).filter(
        (c) => c.campaign_id == null || c.campaign_id !== Number(id)
      )
      setAvailableChars(unassigned)
    } catch (err) {
      setError(err.message)
    } finally {
      setImportLoading(false)
    }
  }

  const handleImportCharacter = async (characterId) => {
    setImportLoading(true)
    try {
      await addCampaignCharacter(id, characterId)
      setShowImport(false)
      loadData()
    } catch (err) {
      setError(err.message)
    } finally {
      setImportLoading(false)
    }
  }

  const handleBeginAdventure = () => {
    setShowLobby(false)
    setShowPlanning(true)
  }

  if (loading) return <Loading />
  if (error && !campaign) return <ErrorMessage message={error} />
  if (!campaign) return <ErrorMessage message="Campaign not found." />

  if (showLobby) {
    return (
      <CampaignLobby
        campaign={campaign}
        currentUser={user}
        onBegin={handleBeginAdventure}
      />
    )
  }

  if (showPlanning && !session) {
    return (
      <CharacterPlanningMode
        campaign={campaign}
        currentUser={user}
        onComplete={loadData}
      />
    )
  }

  const gradient = getGradientSeed(campaign.name + (campaign.seed || ''))
  const initials = getInitials(campaign.name)
  const diffColor = getDifficultyColor(campaign.difficulty)
  const statusColor = STATUS_COLORS[campaign.status] || '#6b7280'

  return (
    <div className="dashboard-page">
      <div className="dashboard-header">
        <div className="dashboard-header-left">
          <button className="dashboard-back" onClick={() => navigate('/')} title="Back to campaigns">
            <i className="bi bi-arrow-left"></i>
          </button>
          <div className="dashboard-avatar" style={{ background: gradient }}>
            {initials}
          </div>
          <div className="dashboard-header-meta">
            <h2>{campaign.name}</h2>
            <div className="dashboard-header-tags">
              {campaign.difficulty && (
                <span className="campaign-badge" style={{ '--badge-color': diffColor }}>
                  {campaign.difficulty}
                </span>
              )}
              <span className="dashboard-status-dot" style={{ background: statusColor }} />
              <span style={{ color: statusColor, fontSize: 13, textTransform: 'capitalize' }}>
                {campaign.status}
              </span>
              <span className="dashboard-date">Created {formatDate(campaign.created_at)}</span>
            </div>
          </div>
        </div>
        <div className="dashboard-header-right">
          <button className="dashboard-settings-btn" onClick={() => {
            setCampaignName(campaign.name)
            setCampaignDesc(campaign.description || '')
            setShowSettings(true)
          }} title="Campaign settings">
            <i className="bi bi-gear-fill"></i>
          </button>
        </div>
      </div>

      {campaign.description && (
        <div className="dashboard-desc-bar">
          {campaign.description}
        </div>
      )}

      <div className="dashboard-layout">
        <aside className="dashboard-left">
          <div className="dashboard-section-title">
            Party Roster
            <div className="roster-actions">
              <button
                className="btn btn-secondary small"
                onClick={() => navigate(`/characters/new?campaign=${id}`)}
                title="Create a new character for this campaign"
              >
                <i className="bi bi-plus-lg"></i> New
              </button>
              <button
                className="btn btn-primary small"
                onClick={openImport}
                title="Import an existing character"
              >
                <i className="bi bi-download"></i> Import
              </button>
            </div>
          </div>
          <PartyRoster characters={characters} campaignId={id} onImport={openImport} />

        </aside>

        <main className="dashboard-center">
          <SessionPanel
            session={session}
            messages={messages}
            onStartSession={handleStartSession}
            onEndSession={handleEndSession}
            onSendMessage={handleSendMessage}
            onRollDice={handleRollDice}
            aiThinking={aiThinking}
          />
        </main>

        <aside className="dashboard-right">
          <div className="dashboard-sidebar-panel"></div>
        </aside>
      </div>

      {showSettings && (
        <div className="modal-overlay" onClick={() => setShowSettings(false)}>
          <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Campaign Settings</h2>
                <button className="modal-close" onClick={() => setShowSettings(false)}><i className="bi bi-x-lg"></i></button>
            </div>
            <div className="campaign-form-compact">
              <div className="form-field">
                <label>Campaign Name</label>
                <input className="input" value={campaignName} onChange={(e) => setCampaignName(e.target.value)} />
              </div>
              <div className="form-field">
                <label>Description</label>
                <textarea className="textarea" value={campaignDesc} onChange={(e) => setCampaignDesc(e.target.value)} rows={4} />
              </div>
              <div className="form-actions-compact">
                <button className="btn btn-secondary" onClick={() => setShowSettings(false)}>Cancel</button>
                <button className="btn btn-primary" onClick={handleUpdateSettings}>Save</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {showImport && (
        <div className="modal-overlay" onClick={() => setShowImport(false)}>
          <div className="modal-panel import-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Import Character</h2>
                <button className="modal-close" onClick={() => setShowImport(false)}><i className="bi bi-x-lg"></i></button>
            </div>
            <div className="import-modal-body">
              {importLoading && availableChars.length === 0 ? (
                <div className="loading" style={{ padding: 24 }}>Loading characters...</div>
              ) : availableChars.length === 0 ? (
                <div className="empty-state-v2" style={{ padding: 24 }}>
                  <p>No available characters to import.</p>
                  <button className="btn btn-primary small" onClick={() => { setShowImport(false); navigate(`/characters/new?campaign=${id}`) }}>
                    Create New Character
                  </button>
                </div>
              ) : (
                <div className="import-char-list">
                  {availableChars.map((c) => {
                    const cg = getGradientSeed(c.name)
                    const ci = getInitials(c.name)
                    return (
                      <button
                        key={c.id}
                        className="import-char-row"
                        onClick={() => handleImportCharacter(c.id)}
                        disabled={importLoading}
                      >
                        <div className="import-char-avatar" style={{ background: cg }}>{ci}</div>
                        <div className="import-char-info">
                          <div className="import-char-name">{c.name}</div>
                          <div className="import-char-meta">{c.race} &middot; {getClassSummary(c)}</div>
                        </div>
                        <i className="bi bi-chevron-right import-char-arrow"></i>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
