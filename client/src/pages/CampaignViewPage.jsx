import { useEffect, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getCampaign, updateCampaign, deleteCampaign, getCampaignCharacters,
  startSession, endSession, getSession, sendMessage,
  getCharacters,
  addCampaignCharacter,
  listMembers,
  getCampaignPlanning,
  getCampaignWorld,
  getSheetProposals,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import PartyRoster from '../components/dashboard/PartyRoster'
import SessionPanel from '../components/dashboard/SessionPanel'
import CampaignLobby from '../components/dashboard/CampaignLobby'
import CharacterPlanningMode from '../components/dashboard/CharacterPlanningMode'
import WorldBuildingMode from '../components/dashboard/WorldBuildingMode'

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

function optimisticPlayerMessage(sessionId, content, user) {
  return {
    id: `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    session_id: sessionId,
    user_id: user?.id || null,
    username: user?.username || null,
    role: 'player',
    content,
    created_at: new Date().toISOString(),
    is_pending: true,
  }
}

function reconcilePendingMessage(messages, pendingId, serverMessages = []) {
  const hasPending = messages.some((message) => message.id === pendingId)
  if (!hasPending) return messages

  const next = []
  messages.forEach((message) => {
    if (message.id === pendingId) {
      next.push(...serverMessages)
    } else {
      next.push(message)
    }
  })
  return next
}

function proposalsToMessages(sessionId, proposals) {
  return (proposals || [])
    .filter((p) => p.status === 'pending')
    .map((p) => ({
      id: `proposal-${p.id}`,
      session_id: sessionId,
      role: 'system',
      content: '',
      is_proposal: true,
      proposal: p,
      created_at: p.created_at,
    }))
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
  let messages = []
  let sheetProposals = []
  if (activeSession) {
    const data = await getSession(activeSession.id).catch(() => ({ session: { messages: [] } }))
    messages = data.session?.messages || []
    const propData = await getSheetProposals(activeSession.id).catch(() => ({ sheet_proposals: [] }))
    sheetProposals = propData.sheet_proposals || []
  }

  return {
    campaign,
    characters: charData.characters || [],
    activeSession,
    messages,
    sheetProposals,
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
  const [deletingCampaign, setDeletingCampaign] = useState(false)
  const [showLobby, setShowLobby] = useState(false)
  const [showPlanning, setShowPlanning] = useState(false)
  const [showWorldBuilding, setShowWorldBuilding] = useState(false)

  const [showImport, setShowImport] = useState(false)
  const [availableChars, setAvailableChars] = useState([])
  const [importLoading, setImportLoading] = useState(false)
  const [aiThinking, setAiThinking] = useState(false)
  const [sheetProposals, setSheetProposals] = useState([])

  const loadData = useCallback(async () => {
    try {
      const data = await fetchCampaignPageData(id)
      setCampaign(data.campaign)
      setCharacters(data.characters)
      setSession(data.activeSession)
      setMessages(data.messages)
      const loadedProposals = data.sheetProposals || []
      setSheetProposals(loadedProposals)
      if (data.activeSession && loadedProposals.length) {
        setMessages((prev) => {
          const existing = new Set(prev.map((m) => m.id))
          const newOnes = proposalsToMessages(data.activeSession.id, loadedProposals).filter((m) => !existing.has(m.id))
          return [...prev, ...newOnes]
        })
      }
      const required = data.campaign.settings?.required_players || 1
      const memData = await listMembers(id)
      const memberCount = (memData.members || []).length
      if (!data.activeSession && memberCount < required) {
        setShowLobby(true)
        setShowPlanning(false)
        setShowWorldBuilding(false)
      } else if (!data.activeSession) {
        const planningData = await getCampaignPlanning(id)
        const allReady = Boolean(planningData.planning?.all_ready)
        let needsWorldBuild = false
        if (allReady) {
          const worldData = await getCampaignWorld(id)
          needsWorldBuild = !worldData.world?.approved_at
        }
        setShowLobby(false)
        setShowPlanning(!allReady)
        setShowWorldBuilding(allReady && needsWorldBuild)
      } else {
        setShowLobby(false)
        setShowPlanning(false)
        setShowWorldBuilding(false)
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
        const loadedProposals = data.sheetProposals || []
        setSheetProposals(loadedProposals)
        if (data.activeSession && loadedProposals.length) {
          setMessages((prev) => {
            const existing = new Set(prev.map((m) => m.id))
            const newOnes = proposalsToMessages(data.activeSession.id, loadedProposals).filter((m) => !existing.has(m.id))
            return [...prev, ...newOnes]
          })
        }

        const required = data.campaign.settings?.required_players || 1
        try {
          const memData = await listMembers(id)
          if (!isMounted) return
          const memberCount = (memData.members || []).length
          if (!data.activeSession && memberCount < required) {
            setShowLobby(true)
            setShowPlanning(false)
            setShowWorldBuilding(false)
          } else if (!data.activeSession) {
            const planningData = await getCampaignPlanning(id)
            if (!isMounted) return
            const allReady = Boolean(planningData.planning?.all_ready)
            let needsWorldBuild = false
            if (allReady) {
              const worldData = await getCampaignWorld(id)
              if (!isMounted) return
              needsWorldBuild = !worldData.world?.approved_at
            }
            setShowLobby(false)
            setShowPlanning(!allReady)
            setShowWorldBuilding(allReady && needsWorldBuild)
          } else {
            setShowLobby(false)
            setShowPlanning(false)
            setShowWorldBuilding(false)
          }
        } catch {
          // If planning fetch fails, still show dashboard with the start-session backend gate.
          if (isMounted) {
            setShowLobby(false)
            setShowPlanning(false)
            setShowWorldBuilding(false)
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
      setMessages(newSession.messages || [])
      setShowWorldBuilding(false)
      const propData = await getSheetProposals(newSession.id).catch(() => ({ sheet_proposals: [] }))
      setSheetProposals(propData.sheet_proposals || [])
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
    const pendingMessage = optimisticPlayerMessage(session.id, content, user)
    setMessages((prev) => [...prev, pendingMessage])
    setAiThinking(true)
    try {
      const data = await sendMessage(session.id, content)
      const newMessages = data.messages || []
      setMessages((prev) => reconcilePendingMessage(prev, pendingMessage.id, newMessages))
      if (data.sheet_proposals?.length) {
        setSheetProposals((prev) => {
          const existing = new Set(prev.map((p) => p.id))
          const newOnes = data.sheet_proposals.filter((p) => !existing.has(p.id))
          return [...prev, ...newOnes]
        })
        const proposalMessages = data.sheet_proposals.map((p) => ({
          id: `proposal-${p.id}`,
          session_id: session.id,
          role: 'system',
          content: '',
          is_proposal: true,
          proposal: p,
          created_at: p.created_at,
        }))
        setMessages((prev) => {
          const existing = new Set(prev.map((m) => m.id))
          const newOnes = proposalMessages.filter((m) => !existing.has(m.id))
          return [...prev, ...newOnes]
        })
      }
    } catch (err) {
      const serverMessages = err.data?.messages || []
      setMessages((prev) => {
        if (serverMessages.length) {
          return reconcilePendingMessage(prev, pendingMessage.id, serverMessages)
        }
        return prev.filter((message) => message.id !== pendingMessage.id)
      })
      setError(err.message)
    } finally {
      setAiThinking(false)
    }
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

  const handleDeleteCampaign = async () => {
    if (!campaign) return
    const confirmed = window.confirm(
      `Delete "${campaign.name}"? This removes the campaign, sessions, invites, planning notes, and world data. Characters in the campaign will not be deleted.`
    )
    if (!confirmed) return

    setDeletingCampaign(true)
    setError('')
    try {
      await deleteCampaign(id)
      navigate('/')
    } catch (err) {
      setError(err.message)
      setDeletingCampaign(false)
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

  const currentCharacter = characters.find((c) => c.user_id === user?.id)

  const handleProposalApplied = (appliedProposal, updatedCharacter) => {
    setSheetProposals((prev) => prev.filter((p) => p.id !== appliedProposal.id))
    setMessages((prev) =>
      prev.map((m) =>
        m.id === `proposal-${appliedProposal.id}`
          ? { ...m, proposal: { ...m.proposal, status: 'applied' } }
          : m
      )
    )
    setCharacters((prev) =>
      prev.map((c) => (c.id === updatedCharacter?.id ? updatedCharacter : c))
    )
  }

  const handleProposalDismissed = (dismissedProposal) => {
    setSheetProposals((prev) => prev.filter((p) => p.id !== dismissedProposal.id))
    setMessages((prev) =>
      prev.map((m) =>
        m.id === `proposal-${dismissedProposal.id}`
          ? { ...m, proposal: { ...m.proposal, status: 'dismissed' } }
          : m
      )
    )
  }

  const handleBeginAdventure = () => {
    setShowLobby(false)
    setShowPlanning(true)
    setShowWorldBuilding(false)
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

  if (showWorldBuilding && !session) {
    return (
      <WorldBuildingMode
        campaign={campaign}
        onBegin={handleStartSession}
        onBack={() => navigate('/')}
      />
    )
  }

  const isOwner = campaign.user_id === user?.id

  return (
    <div className="dashboard-page">
      <div className="dashboard-layout">
        <aside className="dashboard-left">
          <PartyRoster characters={characters} campaignId={id} onImport={openImport} />

        </aside>

        <main className="dashboard-center">
          <SessionPanel
            session={session}
            messages={messages}
            currentUser={user}
            currentCharacter={currentCharacter}
            onStartSession={handleStartSession}
            onEndSession={handleEndSession}
            onSendMessage={handleSendMessage}
            aiThinking={aiThinking}
            sheetProposals={sheetProposals}
            onProposalApplied={handleProposalApplied}
            onProposalDismissed={handleProposalDismissed}
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
              {isOwner && (
                <div className="campaign-danger-zone">
                  <div>
                    <h3>Delete campaign</h3>
                    <p>Remove this campaign and its dashboard history. Characters stay in your character list.</p>
                  </div>
                  <button
                    className="btn btn-danger"
                    onClick={handleDeleteCampaign}
                    disabled={deletingCampaign}
                  >
                    {deletingCampaign ? 'Deleting...' : 'Delete'}
                  </button>
                </div>
              )}
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
