import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import {
  getCampaign, updateCampaign, deleteCampaign, exportCampaign, getCampaignCharacters,
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
import LlmPlayerManager from '../components/dashboard/LlmPlayerManager'
import SheetProposalPopup from '../components/session/SheetProposalPopup'

const SESSION_MESSAGE_PAGE_SIZE = 50

function getInitials(name) {
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
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
  const [campData, charData, worldData] = await Promise.all([
    getCampaign(id),
    getCampaignCharacters(id),
    getCampaignWorld(id).catch(() => ({ world: null })),
  ])
  const campaign = campData.campaign
  const activeSession = campaign.active_session || null
  const currentScene = worldData.world?.current_scene || null
  const worldTitle = worldData.world?.public_intro?.title || ''
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
      currentScene,
      worldTitle,
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
    currentScene,
    worldTitle,
    messages,
    hasOlderMessages: false,
    sheetProposals,
    encounterMap,
  }
}

export default function CampaignViewPage({ user }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [campaign, setCampaign] = useState(null)
  const [currentScene, setCurrentScene] = useState(null)
  const [worldTitle, setWorldTitle] = useState('')
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
  const [exportingCampaign, setExportingCampaign] = useState(false)
  const [showLobby, setShowLobby] = useState(false)
  const [showPlanning, setShowPlanning] = useState(false)
  const [showWorldBuilding, setShowWorldBuilding] = useState(false)
  const [showLootStash, setShowLootStash] = useState(false)
  const [showShops, setShowShops] = useState(false)
  const [elapsedTime, setElapsedTime] = useState('00:00:00')
  const [activeTab, setActiveTab] = useState('chat')

  useEffect(() => {
    if (!session) {
      setElapsedTime('00:00:00')
      return
    }
    const startTime = new Date(session.created_at || session.started_at || Date.now()).getTime()
    const interval = setInterval(() => {
      const diff = Date.now() - startTime
      const hrs = String(Math.floor(diff / 3600000)).padStart(2, '0')
      const mins = String(Math.floor((diff % 3600000) / 60000)).padStart(2, '0')
      const secs = String(Math.floor((diff % 60000) / 1000)).padStart(2, '0')
      setElapsedTime(`${hrs}:${mins}:${secs}`)
    }, 1000)
    return () => clearInterval(interval)
  }, [session])

  const pendingMessageIdsRef = useRef(new Set())

  const [showImport, setShowImport] = useState(false)
  const [availableChars, setAvailableChars] = useState([])
  const [importLoading, setImportLoading] = useState(false)
  const [aiThinking, setAiThinking] = useState(false)
  const [aiThinkingStatus, setAiThinkingStatus] = useState('')
  const [sheetProposals, setSheetProposals] = useState([])
  const [encounterMap, setEncounterMap] = useState(null)
  const [encounterMapLoading, setEncounterMapLoading] = useState(false)
  const [showProposalPopup, setShowProposalPopup] = useState(false)
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
  const showLlmTools = searchParams.get('llm') === 'true'

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
      setCurrentScene(data.currentScene)
      setWorldTitle(data.worldTitle)
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
        setCurrentScene(data.currentScene)
        setWorldTitle(data.worldTitle)
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

  const handleExportCampaign = async () => {
    if (!campaign) return
    setExportingCampaign(true)
    setError('')
    try {
      const blob = await exportCampaign(id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${campaign.name.replace(/[^\w\-]/g, '_')}_export.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(err.message || 'Export failed.')
    } finally {
      setExportingCampaign(false)
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
  const locationName = currentScene?.location_name || campaign?.settings?.current_location || '<insert location here>'

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

  const handleLlmPlayerAdded = () => {
    loadData()
  }

  if (loading) return <Loading />
  if (error && !campaign) return <ErrorMessage message={error} />
  if (!campaign) return <ErrorMessage message="Campaign not found." />

  const isOwner = campaign.user_id === user?.id

  const exportBtn = isOwner && (
    <button
      id="export-campaign-btn"
      className="btn btn-secondary campaign-export-fab"
      onClick={handleExportCampaign}
      disabled={exportingCampaign}
      title="Export campaign data as ZIP"
    >
      <i className="bi bi-download"></i>
      {exportingCampaign ? 'Exporting…' : 'Export'}
    </button>
  )

  if (showLobby) {
    return (
      <>
        <CampaignLobby
          campaign={campaign}
          currentUser={user}
          onBegin={handleBeginAdventure}
          showLlmTools={showLlmTools}
          onLlmPlayerAdded={handleLlmPlayerAdded}
        />
        {exportBtn}
      </>
    )
  }

  if (showPlanning && !session) {
    return (
      <>
        <CharacterPlanningMode
          campaign={campaign}
          currentUser={user}
          onComplete={loadData}
          showLlmTools={showLlmTools}
          onLlmPlayerAdded={handleLlmPlayerAdded}
        />
        {exportBtn}
      </>
    )
  }

  if (showWorldBuilding && !session) {
    return (
      <>
        <WorldBuildingMode
          campaign={campaign}
          onBegin={handleStartSession}
          onBack={() => navigate('/')}
        />
        {exportBtn}
      </>
    )
  }
  const hasActiveMap = isEncounterActive || Boolean(encounterMap)

  return (
    <div className={`dashboard-page ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
      <div className={`dashboard-layout mobile-tab-${activeTab} ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
        <aside className="dashboard-left">
          <div className="campaign-logo-header">
            <div className="campaign-logo">
              <i className="bi bi-hexagon-fill"></i>
            </div>
            <div className="campaign-logo-details">
              <span className="campaign-logo-title">{campaign?.name || 'SALT & SONG'}</span>
              <span className="campaign-logo-subtitle">{worldTitle}</span>
            </div>
          </div>

          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '20px' }}>
            <PartyRoster characters={characters} campaignId={id} onImport={openImport} />
            
            <div className="sidebar-nav">
              <button className="sidebar-nav-item" onClick={() => setShowSettings(true)}>
                <i className="bi bi-gear"></i> Campaign Settings
              </button>
              <button className="sidebar-nav-item" onClick={() => navigate('/characters')}>
                <i className="bi bi-person-badge"></i> Characters
              </button>
              <button className="sidebar-nav-item" onClick={() => setShowWorldBuilding(true)}>
                <i className="bi bi-journal-bookmark"></i> World Journal
              </button>
              <button className="sidebar-nav-item">
                <i className="bi bi-clock-history"></i> Session History
              </button>
            </div>
          </div>

          <div className="sidebar-profile-card">
            <div className="sidebar-profile-avatar">
              {getInitials(user?.username || 'Player')}
            </div>
            <div className="sidebar-profile-info">
              <span className="sidebar-profile-name">{user?.username || 'Guest'}</span>
              <span className="sidebar-profile-role">{isOwner ? 'Host' : 'Player'}</span>
            </div>
            <div className="sidebar-profile-actions">
              <button className="sidebar-profile-action-btn" onClick={() => setShowSettings(true)}>
                <i className="bi bi-gear-fill"></i>
              </button>
              <button
                className="sidebar-profile-action-btn"
                onClick={() => navigate('/')}
                title="Exit to campaigns"
                aria-label="Exit to campaigns"
              >
                <i className="bi bi-door-open"></i>
              </button>
            </div>
          </div>
        </aside>

        <main className={`dashboard-center ${isMapExpanded && hasActiveMap ? 'map-expanded' : ''}`}>
          <div className="dashboard-top-header">
            <div className="session-progress-indicator">
              <span className="pulse-dot"></span>
              <span>SESSION IN PROGRESS</span>
              <span className="timer-text">{elapsedTime}</span>
            </div>
            
            <div className="current-location-dropdown">
              <span className="location-name">{locationName}</span>
              <span className="location-type">Exploration</span>
              <i className="bi bi-chevron-down"></i>
            </div>

            <div className="header-actions">
              <button className="header-icon-btn" onClick={() => setIsMapExpanded(!isMapExpanded)} title="Toggle Map View">
                <i className="bi bi-map"></i>
              </button>
              {session ? (
                <button className="btn btn-primary end-session-btn" onClick={handleEndSession}>End Session</button>
              ) : (
                <button className="btn btn-primary start-session-btn" onClick={handleStartSession}>Start Session</button>
              )}
            </div>
          </div>

          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden', position: 'relative' }}>
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
              characters={characters}
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
          </div>
        </main>

        <aside className="dashboard-right">
          <LlmPlayerManager
            campaignId={id}
            enabled={showLlmTools}
            isOwner={isOwner}
            onAdded={handleLlmPlayerAdded}
          />

          {sheetProposals.length > 0 && (
            <div className="right-sidebar-widget pending-updates-panel">
              <div className="widget-header">
                <h3>Pending Updates <span className="updates-count-badge">{sheetProposals.length}</span></h3>
              </div>
              <div className="updates-list">
                {sheetProposals.map((proposal) => {
                  const char = characters.find((c) => c.id === proposal.character_id)
                  const charName = char ? char.name : 'Unknown Character'
                  const cg = getGradientSeed(charName)
                  const ci = getInitials(charName)
                  return (
                    <div key={proposal.id} className="update-row">
                      <div className="update-avatar" style={{ background: cg }}>
                        {ci}
                      </div>
                      <div className="update-details">
                        <span className="update-char-name">{charName}</span>
                        <span className="update-reason">{proposal.reason}</span>
                      </div>
                      <button className="btn btn-secondary small review-btn" onClick={() => setShowProposalPopup(true)}>
                        Review
                      </button>
                    </div>
                  )
                })}
                <button className="view-all-updates-row" onClick={() => setShowProposalPopup(true)}>
                  <span>View all updates</span>
                  <i className="bi bi-chevron-right"></i>
                </button>
              </div>
            </div>
          )}
          
          <LootBoxStash
            campaignId={id}
            isOwner={isOwner}
            characters={characters}
            onLootBoxOpened={handleLootBoxOpened}
            onViewAll={() => setShowLootStash(true)}
          />

          {(() => {
            const activityLogs = [];
            messages.forEach(m => {
              if (m.is_proposal) {
                activityLogs.push({
                  id: m.id,
                  user: 'System',
                  text: `Proposed update: ${m.proposal?.reason || 'Sheet changes'}`,
                  time: m.created_at,
                  avatar: '⚙️'
                });
              } else if (m.content && m.content.includes('[Roll:')) {
                const rollMatch = m.content.match(/\[Roll:\s*([^\]]+)\]\s*total:\s*(-?\d+)/i);
                const rollName = rollMatch ? rollMatch[1] : 'Dice Roll';
                const total = rollMatch ? rollMatch[2] : '';
                activityLogs.push({
                  id: m.id,
                  user: m.username || 'Player',
                  text: `rolled ${rollName} ${total ? `(Total: ${total})` : ''}`,
                  time: m.created_at,
                  avatar: '🎲'
                });
              } else if (m.content && m.content.includes('[Turn Ended]')) {
                const charName = m.content.replace('[Turn Ended]', '').trim();
                activityLogs.push({
                  id: m.id,
                  user: charName,
                  text: `ended turn`,
                  time: m.created_at,
                  avatar: '⏳'
                });
              }
            });

            const displayLogs = activityLogs.slice(-3).reverse();

            return (
              <div className="right-sidebar-widget recent-activity-panel">
                <div className="widget-header">
                  <h3>Recent Activity</h3>
                  <button className="view-all-link" style={{ background: 'none', border: 'none', cursor: 'pointer' }}>View All</button>
                </div>
                <div className="activity-list">
                  {displayLogs.length > 0 ? (
                    displayLogs.map((log) => (
                      <div key={log.id} className="activity-row">
                        <div className="activity-avatar">{log.avatar}</div>
                        <div className="activity-details">
                          <strong>{log.user}</strong> {log.text}
                        </div>
                        <span className="activity-time">{formatTime(log.time)}</span>
                      </div>
                    ))
                  ) : (
                    <>
                      <div className="activity-row">
                        <div className="activity-avatar">🎲</div>
                        <div className="activity-details">
                          <strong>Kael Thorn</strong> rolled Stealth 18
                        </div>
                        <span className="activity-time">2m ago</span>
                      </div>
                      <div className="activity-row">
                        <div className="activity-avatar">🎲</div>
                        <div className="activity-details">
                          <strong>Pip Quickfoot</strong> rolled Perception 12
                        </div>
                        <span className="activity-time">5m ago</span>
                      </div>
                      <div className="activity-row">
                        <div className="activity-avatar">👑</div>
                        <div className="activity-details">
                          <strong>Dungeon Master</strong> created encounter
                        </div>
                        <span className="activity-time">8m ago</span>
                      </div>
                    </>
                  )}
                </div>
              </div>
            );
          })()}
        </aside>
      </div>
      {exportBtn}

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
                {isOwner && (
                  <button
                    className="btn btn-secondary"
                    onClick={handleExportCampaign}
                    disabled={exportingCampaign}
                    title="Download all campaign data as a ZIP archive"
                  >
                    <i className="bi bi-download"></i>
                    {exportingCampaign ? 'Exporting...' : 'Export'}
                  </button>
                )}
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

      {showProposalPopup && sheetProposals.length > 0 && (
        <SheetProposalPopup
          proposals={sheetProposals}
          sessionId={session?.id}
          onApplied={(prop, char) => {
            handleProposalApplied(prop, char)
            if (sheetProposals.length <= 1) setShowProposalPopup(false)
          }}
          onDismissed={(prop) => {
            handleProposalDismissed(prop)
            if (sheetProposals.length <= 1) setShowProposalPopup(false)
          }}
        />
      )}

      {/* Mobile Bottom Navigation Bar */}
      <nav className="mobile-bottom-nav">
        <button className={`mobile-nav-item ${activeTab === 'chat' ? 'active' : ''}`} onClick={() => setActiveTab('chat')}>
          <i className="bi bi-chat-left-text-fill"></i>
          <span>Chat</span>
        </button>
        {hasActiveMap && (
          <button className={`mobile-nav-item ${activeTab === 'map' ? 'active' : ''}`} onClick={() => setActiveTab('map')}>
            <i className="bi bi-map-fill"></i>
            <span>Map</span>
          </button>
        )}
        <button className={`mobile-nav-item ${activeTab === 'party' ? 'active' : ''}`} onClick={() => setActiveTab('party')}>
          <i className="bi bi-people-fill"></i>
          <span>Party</span>
        </button>
        <button className={`mobile-nav-item ${activeTab === 'menu' ? 'active' : ''}`} onClick={() => setActiveTab('menu')}>
          <i className="bi bi-grid-fill"></i>
          <span>Menu</span>
          {sheetProposals.length > 0 && (
            <span className="menu-badge">{sheetProposals.length}</span>
          )}
        </button>
      </nav>
    </div>
  )
}
