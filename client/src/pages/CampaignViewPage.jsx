import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getCampaign, updateCampaign, deleteCampaign, getCampaignCharacters,
  startSession, endSession, getSession, getMessages, sendMessage,
  getCharacters,
  addCampaignCharacter,
  listMembers,
  getCampaignPlanning,
  getCampaignWorld,
  getCurrentEncounterMap,
  getSheetProposals,
  rollPlayerInitiative,
  getSessionStreamUrl,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import PartyRoster from '../components/dashboard/PartyRoster'
import SessionPanel from '../components/dashboard/SessionPanel'
import LootBoxStash from '../components/lootbox/LootBoxStash'
import CampaignLobby from '../components/dashboard/CampaignLobby'
import CampaignShops from '../components/shop/CampaignShops'
import CharacterPlanningMode from '../components/dashboard/CharacterPlanningMode'
import EncounterMapPanel from '../components/dashboard/EncounterMapPanel'
import WorldBuildingMode from '../components/dashboard/WorldBuildingMode'

const SESSION_MESSAGE_PAGE_SIZE = 50

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
  if (!hasPending) return mergeUniqueMessages(messages, serverMessages)

  const serverIds = new Set(serverMessages.map((message) => message.id))
  const insertedIds = new Set()
  const next = []
  messages.forEach((message) => {
    if (message.id === pendingId) {
      serverMessages.forEach((serverMessage) => {
        if (!insertedIds.has(serverMessage.id)) {
          next.push(serverMessage)
          insertedIds.add(serverMessage.id)
        }
      })
    } else if (!serverIds.has(message.id)) {
      next.push(message)
    }
  })
  return next
}

function mergeUniqueMessages(messages, additions = []) {
  if (!additions.length) return messages
  const existing = new Set(messages.map((message) => message.id))
  const next = [...messages]
  additions.forEach((message) => {
    if (!existing.has(message.id)) {
      next.push(message)
      existing.add(message.id)
    }
  })
  return next
}

function uniqueMessages(messages = []) {
  const existing = new Set()
  const next = []
  messages.forEach((message) => {
    if (!existing.has(message.id)) {
      next.push(message)
      existing.add(message.id)
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
  let encounterMap = null
  if (activeSession) {
    const [data, propData, mapData] = await Promise.all([
      getSession(activeSession.id, { limit: SESSION_MESSAGE_PAGE_SIZE }).catch(() => ({ session: { messages: [], has_more_messages: false } })),
      getSheetProposals(activeSession.id).catch(() => ({ sheet_proposals: [] })),
      getCurrentEncounterMap(id).catch(() => ({ encounter_map: null })),
    ])
    messages = data.session?.messages || []
    const hasOlderMessages = Boolean(data.session?.has_more_messages)
    sheetProposals = propData.sheet_proposals || []
    encounterMap = mapData.encounter_map || null
    return {
      campaign,
      characters: charData.characters || [],
      activeSession,
      messages,
      hasOlderMessages,
      sheetProposals,
      encounterMap,
    }
  }

  return {
    campaign,
    characters: charData.characters || [],
    activeSession,
    messages,
    hasOlderMessages: false,
    sheetProposals,
    encounterMap,
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
  const [hasOlderMessages, setHasOlderMessages] = useState(false)
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [campaignName, setCampaignName] = useState('')
  const [campaignDesc, setCampaignDesc] = useState('')
  const [deletingCampaign, setDeletingCampaign] = useState(false)
  const [showLobby, setShowLobby] = useState(false)
  const [showPlanning, setShowPlanning] = useState(false)
  const [showWorldBuilding, setShowWorldBuilding] = useState(false)
  const [showLootStash, setShowLootStash] = useState(false)
  const [showShops, setShowShops] = useState(false)
  const pendingMessageIdsRef = useRef(new Set())

  const [showImport, setShowImport] = useState(false)
  const [availableChars, setAvailableChars] = useState([])
  const [importLoading, setImportLoading] = useState(false)
  const [aiThinking, setAiThinking] = useState(false)
  const [aiThinkingStatus, setAiThinkingStatus] = useState('')
  const [sheetProposals, setSheetProposals] = useState([])
  const [encounterMap, setEncounterMap] = useState(null)
  const [encounterMapLoading, setEncounterMapLoading] = useState(false)
  const [isMapExpanded, setIsMapExpanded] = useState(() => {
    return localStorage.getItem('encounter_map_collapsed') === 'false'
  })
  const isEncounterActive = useMemo(() => {
    if (campaign?.settings?.encounter_active) return true
    if (!encounterMap) return false
    const rawState = encounterMap.encounter_state || encounterMap.encounter_state_json
    if (!rawState) return false
    try {
      const parsed = typeof rawState === 'string' ? JSON.parse(rawState) : rawState
      return Boolean(parsed?.active)
    } catch {
      return false
    }
  }, [campaign?.settings?.encounter_active, encounterMap])

  const hasPlacements = useMemo(() => {
    return Boolean(encounterMap?.placements && encounterMap.placements.length > 0)
  }, [encounterMap])

  useEffect(() => {
    if (isEncounterActive && hasPlacements) {
      setIsMapExpanded(true)
      localStorage.setItem('encounter_map_collapsed', 'false')
    }
  }, [isEncounterActive, hasPlacements])

  const loadData = useCallback(async () => {
    try {
      const data = await fetchCampaignPageData(id)
      setCampaign(data.campaign)
      setCharacters(data.characters)
      setSession(data.activeSession)
      setMessages(data.messages)
      setHasOlderMessages(data.hasOlderMessages)
      setEncounterMap(data.encounterMap || null)
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
        setHasOlderMessages(data.hasOlderMessages)
        setEncounterMap(data.encounterMap || null)
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

  // Background polling for encounter map when a session is active
  useEffect(() => {
    if (!session) return

    const interval = setInterval(async () => {
      try {
        const mapData = await getCurrentEncounterMap(id)
        setEncounterMap(mapData.encounter_map || null)
      } catch {
        // Silently ignore polling errors to avoid disrupting play
      }
    }, 4000)

    return () => clearInterval(interval)
  }, [id, session])

  // Resilient stream listener on mount/refresh
  useEffect(() => {
    if (!session?.id) return

    let eventSource = null
    let isMounted = true

    const connectStream = () => {
      if (!isMounted) return
      const streamUrl = getSessionStreamUrl(session.id)
      eventSource = new EventSource(streamUrl)

      eventSource.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data)
          if (payload.type === 'status') {
            if (payload.status !== 'idle') {
              setAiThinking(true)
              setAiThinkingStatus(payload.status)
            } else {
              setAiThinking(false)
              setAiThinkingStatus('')
            }
          } else if (payload.type === 'error') {
            setError(payload.error)
            setAiThinking(false)
            setAiThinkingStatus('')
          } else if (payload.type === 'message') {
            if (payload.message) {
              setMessages((prev) => mergeUniqueMessages(prev, [payload.message]))
            }
          } else if (payload.type === 'proposal_applied') {
            setSheetProposals((prev) => prev.filter((p) => p.id !== payload.proposal.id))
            setMessages((prev) =>
              prev.map((m) =>
                m.id === `proposal-${payload.proposal.id}`
                  ? { ...m, proposal: { ...m.proposal, status: 'applied' } }
                  : m
              )
            )
            if (payload.character) {
              setCharacters((prev) =>
                prev.map((c) => (c.id === payload.character.id ? payload.character : c))
              )
            }
          } else if (payload.type === 'proposal_dismissed') {
            setSheetProposals((prev) => prev.filter((p) => p.id !== payload.proposal.id))
            setMessages((prev) =>
              prev.map((m) =>
                m.id === `proposal-${payload.proposal.id}`
                  ? { ...m, proposal: { ...m.proposal, status: 'dismissed' } }
                  : m
              )
            )
          } else if (payload.type === 'refresh') {
            loadData()
          } else if (payload.type === 'done') {
            const serverMessages = payload.messages || []
            if (serverMessages.length) {
              setMessages((prev) => {
                let next = prev
                for (const pendingId of pendingMessageIdsRef.current) {
                  next = reconcilePendingMessage(next, pendingId, serverMessages)
                }
                pendingMessageIdsRef.current.clear()
                return mergeUniqueMessages(next, serverMessages)
              })
            }
            if (payload.sheet_proposals?.length) {
              setSheetProposals((prev) => {
                const existing = new Set(prev.map((p) => p.id))
                const newOnes = payload.sheet_proposals.filter((p) => !existing.has(p.id))
                return [...prev, ...newOnes]
              })
              const proposalMessages = payload.sheet_proposals.map((p) => ({
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
            setAiThinking(false)
            setAiThinkingStatus('')
          }
        } catch (e) {
          console.error("SSE parse error on reconnect", e)
        }
      }

      eventSource.onerror = () => {
        setAiThinking(false)
        setAiThinkingStatus('')
      }
    }

    // Delay connection slightly to make sure page load calls complete
    const timer = setTimeout(connectStream, 500)

    return () => {
      isMounted = false
      clearTimeout(timer)
      if (eventSource) {
        eventSource.close()
      }
    }
  }, [session?.id])

  const handleStartSession = async () => {
    try {
      const data = await startSession(id)
      const newSession = data.session
      setSession(newSession)
      setMessages(newSession.messages || [])
      setHasOlderMessages(false)
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
      setHasOlderMessages(false)
      loadData()
    } catch (err) {
      setError(err.message)
    }
  }

  const handleSendMessage = async (content) => {
    if (!session) return
    const pendingMessage = optimisticPlayerMessage(session.id, content, user)
    setMessages((prev) => [...prev, pendingMessage])
    pendingMessageIdsRef.current.add(pendingMessage.id)
    setAiThinking(true)
    setAiThinkingStatus("Checking safety...")

    try {
      const data = await sendMessage(session.id, content)
      setMessages((prev) => {
        const next = reconcilePendingMessage(prev, pendingMessage.id, data.messages || [])
        pendingMessageIdsRef.current.delete(pendingMessage.id)
        return next
      })

      // Check for initiative roll in content
      const match = content.match(/\[Roll:\s*([^\]]+)\]\s*total:\s*(-?\d+)/i)
      if (match && encounterMap?.id && user?.id) {
        const label = match[1]
        const total = parseInt(match[2], 10)
        if (label.toLowerCase().includes('initiative')) {
          try {
            await rollPlayerInitiative(encounterMap.id, 'player', String(user.id), total)
          } catch (err) {
            console.error('Failed to submit initiative roll:', err)
          }
        }
      }

      setEncounterMapLoading(true)
      getCurrentEncounterMap(id)
        .then((mapData) => setEncounterMap(mapData.encounter_map || null))
        .catch(() => {})
        .finally(() => setEncounterMapLoading(false))

    } catch (err) {
      setAiThinking(false)
      setAiThinkingStatus('')
      const serverMessages = err.data?.messages || []
      setMessages((prev) => {
        if (serverMessages.length) {
          const next = reconcilePendingMessage(prev, pendingMessage.id, serverMessages)
          pendingMessageIdsRef.current.delete(pendingMessage.id)
          return next
        }
        pendingMessageIdsRef.current.delete(pendingMessage.id)
        return prev.filter((message) => message.id !== pendingMessage.id)
      })
      setError(err.message)
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

  const loadOlderMessages = useCallback(async () => {
    if (!session || loadingOlderMessages || !hasOlderMessages) return 0
    const oldestMessage = messages.find((message) => Number.isInteger(message.id))
    if (!oldestMessage) {
      setHasOlderMessages(false)
      return 0
    }

    setLoadingOlderMessages(true)
    try {
      const data = await getMessages(session.id, {
        beforeId: oldestMessage.id,
        limit: SESSION_MESSAGE_PAGE_SIZE,
      })
      const olderMessages = data.messages || []
      setHasOlderMessages(Boolean(data.has_more_messages))
      if (olderMessages.length) {
        setMessages((prev) => {
          const existing = new Set(prev.map((message) => message.id))
          return [
            ...olderMessages.filter((message) => !existing.has(message.id)),
            ...prev,
          ]
        })
      }
      return olderMessages.length
    } catch (err) {
      setError(err.message)
      return 0
    } finally {
      setLoadingOlderMessages(false)
    }
  }, [hasOlderMessages, loadingOlderMessages, messages, session])

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

  const handleLootBoxOpened = async () => {
    loadData() // Always reload campaign data (characters, etc.)

    if (!session) return
    const propData = await getSheetProposals(session.id).catch(() => ({ sheet_proposals: [] }))
    const newProposals = propData.sheet_proposals || []
    setSheetProposals((prev) => {
      const existing = new Set(prev.map((p) => p.id))
      const fresh = newProposals.filter((p) => !existing.has(p.id))
      return [...prev, ...fresh]
    })
    if (newProposals.length) {
      setMessages((prev) => {
        const existing = new Set(prev.map((m) => m.id))
        const proposalMessages = newProposals
          .filter((p) => !existing.has(`proposal-${p.id}`))
          .map((p) => ({
            id: `proposal-${p.id}`,
            session_id: session.id,
            role: 'system',
            content: '',
            is_proposal: true,
            proposal: p,
            created_at: p.created_at,
          }))
        return [...prev, ...proposalMessages]
      })
    }
  }

  const handlePurchaseSuccess = (updatedCharacter) => {
    setCharacters((prev) =>
      prev.map((c) => (c.id === updatedCharacter?.id ? updatedCharacter : c))
    )
    loadData()
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
  const hasActiveMap = isEncounterActive || Boolean(encounterMap)

  return (
    <div className={`dashboard-page ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
      <div className={`dashboard-layout ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
        <aside className="dashboard-left">
          <PartyRoster characters={characters} campaignId={id} onImport={openImport} />
        </aside>

        <main className={`dashboard-center ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
          {hasActiveMap && (
            <EncounterMapPanel
              encounterMap={encounterMap}
              loading={encounterMapLoading}
              isOwner={isOwner}
              currentUser={user}
              currentCharacter={currentCharacter}
              onEncounterMapChange={setEncounterMap}
              isMapExpanded={isMapExpanded}
              setIsMapExpanded={setIsMapExpanded}
              onSendMessage={handleSendMessage}
            />
          )}
          <SessionPanel
            session={session}
            messages={messages}
            currentUser={user}
            currentCharacter={currentCharacter}
            onStartSession={handleStartSession}
            onEndSession={handleEndSession}
            onSendMessage={handleSendMessage}
            hasOlderMessages={hasOlderMessages}
            loadingOlderMessages={loadingOlderMessages}
            onLoadOlderMessages={loadOlderMessages}
            aiThinking={aiThinking}
            aiThinkingStatus={aiThinkingStatus}
            sheetProposals={sheetProposals}
            onProposalApplied={handleProposalApplied}
            onProposalDismissed={handleProposalDismissed}
            onToggleLootStash={() => setShowLootStash(true)}
            onToggleShops={() => setShowShops(true)}
          />
        </main>

        <aside className="dashboard-right">
          <LootBoxStash campaignId={id} isOwner={isOwner} characters={characters} onLootBoxOpened={handleLootBoxOpened} />
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

      {showLootStash && (
        <div className="modal-overlay" onClick={() => setShowLootStash(false)}>
          <div className="modal-panel lootbox-stash-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Loot Stash</h2>
              <button className="modal-close" onClick={() => setShowLootStash(false)}><i className="bi bi-x-lg"></i></button>
            </div>
            <div style={{ padding: '24px', overflowY: 'auto', maxHeight: '70vh' }}>
              <LootBoxStash campaignId={id} isOwner={isOwner} characters={characters} onLootBoxOpened={handleLootBoxOpened} />
            </div>
          </div>
        </div>
      )}

      {showShops && (
        <div className="modal-overlay" onClick={() => setShowShops(false)}>
          <div className="modal-panel" style={{ maxWidth: '900px' }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Local Shops</h2>
              <button className="modal-close" onClick={() => setShowShops(false)}><i className="bi bi-x-lg"></i></button>
            </div>
            <div style={{ padding: '24px', overflowY: 'auto', maxHeight: '78vh' }}>
              <CampaignShops
                campaignId={id}
                currentCharacter={currentCharacter}
                onPurchaseSuccess={handlePurchaseSuccess}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
