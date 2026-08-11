import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  createCharacter,

  getCampaignPlanning,
  getCharacters,
  getPlanningStreamUrl,
  selectPlanningCharacter,
  sendPlanningMessage,
  setPlanningReady,
  updatePlanningBond,
} from '../../api/client'
import Loading from '../common/Loading'
import ErrorMessage from '../common/ErrorMessage'
import MarkdownContent from '../common/MarkdownContent'
import LlmPlayerManager from './LlmPlayerManager'
import {
  CharacterFormBody,
} from '../character/CharacterForm'
import {
  CHARACTER_FORM_PAGES,
  ITEM_LIST_CONFIGS,
  flattenCharacter,
  makeEmptyCharacter,
  mergeCharacterDraft,
  normalizeCharacterDraft,
} from '../character/characterFormConfig'

const VALID_PAGE_KEYS = new Set(CHARACTER_FORM_PAGES.map((page) => page.key))
const ITEM_LIST_IDENTITY_KEYS = {
  proficiencies: 'name',
}
const ITEM_LIST_MERGE_KEYS = Object.fromEntries(
  ITEM_LIST_CONFIGS.map((config) => {
    const labelField = config.fields.find((field) => field.type !== 'checkbox') || config.fields[0]
    return [config.key, ITEM_LIST_IDENTITY_KEYS[config.key] || labelField.key]
  })
)

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

function getClassSummary(character) {
  if (!character) return 'No character selected'
  if (character.classes?.length) {
    return character.classes.map((c) => `${c.class_name} ${c.level}`).join(', ')
  }
  return `Level ${character.total_level ?? 1}`
}

function listFrom(value) {
  if (!value) return []
  if (Array.isArray(value)) return value
  if (typeof value === 'string') return value ? [value] : []
  return []
}

import { parseDate } from '../../utils/date'

function formatTime(iso) {
  if (!iso) return ''
  return parseDate(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function valuesEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b)
}

function isPlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
}

function isEmptyValue(value) {
  if (value == null) return true
  if (typeof value === 'string') return value.trim() === ''
  if (Array.isArray(value)) return value.length === 0
  return false
}

function isPathTouched(touchedPaths, path) {
  if (!path) return false
  const parts = path.split('.')
  return parts.some((_, index) => touchedPaths.has(parts.slice(0, index + 1).join('.')))
}

function setAtPath(source, path, value) {
  const parts = path.split('.')
  const next = Array.isArray(source) ? [...source] : { ...source }
  let cursor = next
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i]
    cursor[part] = Array.isArray(cursor[part]) ? [...cursor[part]] : { ...(cursor[part] || {}) }
    cursor = cursor[part]
  }
  cursor[parts[parts.length - 1]] = value
  return next
}

function listItemMergeKey(path, item) {
  const listKey = path.split('.')[0]
  const fieldKey = ITEM_LIST_MERGE_KEYS[listKey]
  if (!fieldKey || !item || typeof item !== 'object' || Array.isArray(item)) return null
  const value = item[fieldKey]
  if (value === undefined || value === null || String(value).trim() === '') return null
  return String(value).trim().toLowerCase()
}

function mergeItemListPatch(currentValue, patchValue, path) {
  if (!Array.isArray(currentValue) || !Array.isArray(patchValue)) return null
  if (!ITEM_LIST_MERGE_KEYS[path]) return null

  const next = [...currentValue]
  const byKey = new Map()
  next.forEach((item, index) => {
    const key = listItemMergeKey(path, item)
    if (key) byKey.set(key, index)
  })

  patchValue.forEach((item) => {
    const key = listItemMergeKey(path, item)
    if (key && byKey.has(key)) {
      next[byKey.get(key)] = item
    } else {
      if (key) byKey.set(key, next.length)
      next.push(item)
    }
  })

  return next
}

function applySafePatch(current, patch, touchedPaths, baseline, prefix = '') {
  if (!isPlainObject(patch)) return { next: current, suggestions: [] }
  let next = Array.isArray(current) ? [...current] : { ...current }
  const suggestions = []

  Object.entries(patch).forEach(([key, patchValue]) => {
    const path = prefix ? `${prefix}.${key}` : key
    const currentValue = next?.[key]
    const baselineValue = baseline?.[key]
    if (isPlainObject(patchValue) && isPlainObject(currentValue)) {
      const result = applySafePatch(currentValue, patchValue, touchedPaths, baselineValue || {}, path)
      next[key] = result.next
      suggestions.push(...result.suggestions)
      return
    }

    const mergedList = !isPathTouched(touchedPaths, path)
      ? mergeItemListPatch(currentValue, patchValue, path)
      : null
    if (mergedList) {
      next[key] = mergedList
      return
    }

    const canApply = !isPathTouched(touchedPaths, path)
      && (isEmptyValue(currentValue) || valuesEqual(currentValue, baselineValue))
    if (canApply) {
      next[key] = patchValue
    } else if (!valuesEqual(currentValue, patchValue)) {
      suggestions.push({ path, value: patchValue })
    }
  })

  return { next, suggestions }
}

function pathLabel(path) {
  return path
    .split('.')
    .map((part) => part.replaceAll('_', ' '))
    .join(' / ')
}

function planningDraftStorageKey(campaignId, userId) {
  return `campaign-planning-draft:${campaignId}:${userId || 'anonymous'}`
}

function loadStoredDraft(campaignId, user) {
  const fallback = { player_name: user?.username || '' }
  if (typeof window === 'undefined') return fallback

  try {
    const stored = window.localStorage.getItem(planningDraftStorageKey(campaignId, user?.id))
    return stored ? { ...JSON.parse(stored), player_name: user?.username || fallback.player_name } : fallback
  } catch {
    return fallback
  }
}

function saveStoredDraft(campaignId, userId, draft) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(planningDraftStorageKey(campaignId, userId), JSON.stringify(draft))
  } catch {
    // Local storage is best-effort; saving the character still uses the API.
  }
}

function clearStoredDraft(campaignId, userId) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.removeItem(planningDraftStorageKey(campaignId, userId))
  } catch {
    // Ignore unavailable local storage.
  }
}

function pendingBondsForUser(bonds, userId) {
  return (bonds || []).filter((bond) => (
    bond.status === 'pending'
      && (bond.involved_user_ids || []).some((involvedId) => String(involvedId) === String(userId))
  ))
}

export default function CharacterPlanningMode({
  campaign,
  currentUser,
  onComplete,
  showLlmTools = false,
  onLlmPlayerAdded,
}) {
  const navigate = useNavigate()
  const [planning, setPlanning] = useState(null)
  const [flowMode, setFlowMode] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const sendingRef = useRef(false)
  const changeSending = useCallback((value) => {
    setSending(value)
    sendingRef.current = value
  }, [])
  const [savingCharacter, setSavingCharacter] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [availableChars, setAvailableChars] = useState([])
  const [importLoading, setImportLoading] = useState(false)
  const [bondEdits, setBondEdits] = useState({})
  const [draftCharacter, setDraftCharacter] = useState(() => mergeCharacterDraft(
    loadStoredDraft(campaign.id, currentUser)
  ))
  const [activePage, setActivePage] = useState('identity')
  const [touchedPaths, setTouchedPaths] = useState(() => new Set())
  const touchedPathsRef = useRef(touchedPaths)
  useEffect(() => {
    touchedPathsRef.current = touchedPaths
  }, [touchedPaths])
  const [pendingSuggestions, setPendingSuggestions] = useState([])
  const [partyInfoCollapsed, setPartyInfoCollapsed] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const chatMessagesRef = useRef(null)
  const lastSeenMessageIdRef = useRef(null)
  const pendingAutoScrollRef = useRef(false)
  const isAutoScrollEnabledRef = useRef(true)
  const lastAppliedDraftPatchEventIdRef = useRef(null)
  const pendingMessageIdsRef = useRef(new Set())
  const pendingGenerationRef = useRef(false)

  const handleScroll = useCallback(() => {
    const container = chatMessagesRef.current
    if (!container) return
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight
    // Re-enable auto-scroll if scrolled to the bottom (within 20px threshold)
    isAutoScrollEnabledRef.current = distanceFromBottom < 20
  }, [])
  const baselineCharacter = useMemo(() => makeEmptyCharacter(), [])

  const applyFormPatch = useCallback((patch) => {
    if (!patch || Object.keys(patch).length === 0) return
    const normalizedPatch = normalizeCharacterDraft(patch)
    setDraftCharacter((prev) => {
      const result = applySafePatch(prev, normalizedPatch, touchedPathsRef.current, baselineCharacter)
      setPendingSuggestions((existing) => [...existing, ...result.suggestions])
      return result.next
    })
  }, [baselineCharacter])

  const finalizeSending = useCallback(() => {
    pendingGenerationRef.current = false
    setStreamingContent('')
    changeSending(false)
    // Clear any optimistic temp messages still in the feed
    setPlanning((prev) => {
      if (!prev) return prev
      const filtered = (prev.messages || []).filter((msg) => !pendingMessageIdsRef.current.has(msg.id))
      pendingMessageIdsRef.current.clear()
      return { ...prev, messages: filtered }
    })
  }, [changeSending])

  const loadPlanning = useCallback(async ({ quiet = false, onComplete } = {}) => {
    if (!quiet) setLoading(true)
    try {
      const data = await getCampaignPlanning(campaign.id)
      setPlanning(data.planning)
      const draftPatchEventId = data.planning?.draft_patch_event_id
      if (draftPatchEventId && lastAppliedDraftPatchEventIdRef.current !== draftPatchEventId) {
        lastAppliedDraftPatchEventIdRef.current = draftPatchEventId
        applyFormPatch(data.planning?.draft_patch)
      }
      setError('')
      if (onComplete) {
        onComplete()
      } else if (quiet && pendingGenerationRef.current) {
        // Recovery path: if the SSE "done" event was missed (reconnect gap,
        // dropped connection, etc.), detect completion via polling instead of
        // leaving the UI stuck on "Thinking..." indefinitely.
        const messages = data.planning?.messages || []
        const lastMessage = messages[messages.length - 1]
        if (lastMessage?.role === 'dm') {
          finalizeSending()
        }
      }
    } catch (err) {
      setError(err.message)
      // Always finalize a pending send even if the refresh fetch failed, so the
      // chat never deadlocks on a stuck "Thinking..." indicator.
      if (onComplete) onComplete()
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [campaign.id, applyFormPatch, finalizeSending, sendingRef])

  useEffect(() => {
    Promise.resolve().then(() => loadPlanning())
    const interval = setInterval(() => loadPlanning({ quiet: true }), 5000)
    return () => clearInterval(interval)
  }, [loadPlanning])

  // Resilient planning stream listener on mount/refresh
  useEffect(() => {
    if (!campaign?.id) return

    let eventSource = null
    let isMounted = true

    const connectStream = () => {
      if (!isMounted) return
      const streamUrl = getPlanningStreamUrl(campaign.id)
      eventSource = new EventSource(streamUrl)

      eventSource.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data)
          if (payload.type === 'token') {
            changeSending(true)
            pendingGenerationRef.current = true
            setStreamingContent((prev) => prev + payload.token)
            const container = chatMessagesRef.current
            if (container && isAutoScrollEnabledRef.current) {
              window.requestAnimationFrame(() => {
                container.scrollTop = container.scrollHeight
              })
            }
          } else if (payload.type === 'done') {
            if (VALID_PAGE_KEYS.has(payload.active_page)) setActivePage(payload.active_page)
            applyFormPatch(payload.form_patch)
            loadPlanning({
              quiet: true,
              onComplete: finalizeSending,
            })
          } else if (payload.type === 'error') {
            pendingGenerationRef.current = false
            setError(payload.error)
            setStreamingContent('')
            changeSending(false)
          } else if (payload.type === 'planning_refresh') {
            loadPlanning({ quiet: true })
          } else if (payload.type === 'session_started') {
            onComplete()
          }
        } catch (e) {
          console.error('Planning SSE parse error', e)
        }
      }

      eventSource.onerror = () => {
        // If we were actively receiving content, clean up
        pendingGenerationRef.current = false
        changeSending(false)
        setStreamingContent('')
        loadPlanning({ quiet: true })
      }
    }

    connectStream()

    return () => {
      isMounted = false
      if (eventSource) {
        eventSource.close()
      }
    }
  }, [campaign.id, loadPlanning, applyFormPatch, changeSending, finalizeSending, onComplete])

  useEffect(() => {
    saveStoredDraft(campaign.id, currentUser?.id, draftCharacter)
  }, [campaign.id, currentUser?.id, draftCharacter])

  const currentMember = planning?.members?.find((member) => member.user_id === currentUser?.id)
  const selectedCharacter = currentMember?.selected_character
  const isReady = Boolean(currentMember?.is_character_ready)

  useEffect(() => {
    if (selectedCharacter && isReady) {
      clearStoredDraft(campaign.id, currentUser?.id)
    }
  }, [campaign.id, currentUser?.id, selectedCharacter, isReady])

  useEffect(() => {
    const messages = planning?.messages || []
    const lastMessageId = messages.length ? messages[messages.length - 1].id : null

    if (!lastMessageId) {
      lastSeenMessageIdRef.current = null
      pendingAutoScrollRef.current = false
      return
    }

    if (lastSeenMessageIdRef.current === null) {
      lastSeenMessageIdRef.current = lastMessageId
      return
    }

    if (lastSeenMessageIdRef.current === lastMessageId) return

    const container = chatMessagesRef.current
    const shouldAutoScroll = pendingAutoScrollRef.current || isAutoScrollEnabledRef.current

    lastSeenMessageIdRef.current = lastMessageId
    pendingAutoScrollRef.current = false

    if (container && shouldAutoScroll) {
      window.requestAnimationFrame(() => {
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' })
      })
    }
  }, [planning?.messages])

  const markFieldTouched = (path) => {
    setTouchedPaths((prev) => {
      const next = new Set(prev)
      next.add(path)
      return next
    })
  }

  const applySuggestion = (suggestion) => {
    setDraftCharacter((prev) => setAtPath(prev, suggestion.path, suggestion.value))
    setTouchedPaths((prev) => {
      const next = new Set(prev)
      next.add(suggestion.path)
      return next
    })
    setPendingSuggestions((prev) => prev.filter((item) => item !== suggestion))
  }

  const openImport = async () => {
    setShowImport(true)
    setImportLoading(true)
    setError('')
    try {
      const data = await getCharacters()
      setAvailableChars((data.characters || []).filter((c) => c.id !== selectedCharacter?.id))
    } catch (err) {
      setError(err.message)
    } finally {
      setImportLoading(false)
    }
  }

  const handleSelectCharacter = async (characterId) => {
    if (blockingPendingBonds.length > 0) {
      setError('Resolve pending bond proposals before marking ready.')
      return
    }

    setImportLoading(true)
    setError('')
    try {
      const data = await selectPlanningCharacter(campaign.id, characterId)
      setPlanning(data.planning)
      const readyData = await setPlanningReady(campaign.id, true)
      setPlanning(readyData.planning)
      setShowImport(false)
      clearStoredDraft(campaign.id, currentUser?.id)
      setFlowMode('waiting')
    } catch (err) {
      setError(err.message)
    } finally {
      setImportLoading(false)
    }
  }

  const handleChangeCharacter = async () => {
    setError('')
    try {
      await setPlanningReady(campaign.id, false)
      const data = await selectPlanningCharacter(campaign.id, null)
      setPlanning(data.planning)
      setFlowMode('choice')
    } catch (err) {
      setError(err.message)
    }
  }

  const handleReadySelectedCharacter = async () => {
    if (!selectedCharacter) return
    if (blockingPendingBonds.length > 0) {
      setError('Resolve pending bond proposals before marking ready.')
      return
    }

    setImportLoading(true)
    setError('')
    try {
      const readyData = await setPlanningReady(campaign.id, true)
      setPlanning(readyData.planning)
      setFlowMode('waiting')
    } catch (err) {
      setError(err.message)
    } finally {
      setImportLoading(false)
    }
  }

  const handleSend = async () => {
    const content = input.trim()
    if (!content) return
    changeSending(true)
    setInput('')
    setStreamingContent('')
    pendingAutoScrollRef.current = true
    isAutoScrollEnabledRef.current = true

    // Optimistically add the player's message to the local chat feed
    const tempMessage = {
      id: `temp-${Date.now()}`,
      role: 'player',
      content: content,
      created_at: new Date().toISOString(),
    }
    setPlanning((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        messages: [...(prev.messages || []), tempMessage],
      }
    })
    pendingMessageIdsRef.current.add(tempMessage.id)

    try {
      const data = await sendPlanningMessage(campaign.id, content, {
        draftCharacter,
        activePage,
      })

      // If the server returned a synchronous response (test mode), apply it directly
      if (data.planning) {
        setPlanning((prev) => {
          if (!prev) return data.planning
          const filteredMessages = (prev.messages || []).filter((msg) => msg.id !== tempMessage.id)
          pendingMessageIdsRef.current.delete(tempMessage.id)
          return {
            ...data.planning,
            messages: [...filteredMessages, ...(data.planning.messages || [])]
          }
        })
        if (VALID_PAGE_KEYS.has(data.active_page)) setActivePage(data.active_page)
        applyFormPatch(data.form_patch)
        changeSending(false)
        return
      }

      // Production: the server kicked off a streaming generation; the persistent
      // SSE listener will deliver tokens and the final "done" event.
      pendingGenerationRef.current = true
    } catch (err) {
      pendingGenerationRef.current = false
      pendingAutoScrollRef.current = false
      setStreamingContent('')
      setError(err.message)
      changeSending(false)
      setInput(content) // Restore input so they don't lose their message
      setPlanning((prev) => {
        if (!prev) return prev
        return {
          ...prev,
          messages: (prev.messages || []).filter((msg) => msg.id !== tempMessage.id),
        }
      })
      pendingMessageIdsRef.current.delete(tempMessage.id)
    }
  }

  const handleSaveCharacter = async (event) => {
    event.preventDefault()
    if (blockingPendingBonds.length > 0) {
      setError('Resolve pending bond proposals before marking ready.')
      return
    }
    if (!draftCharacter.name?.trim() || !draftCharacter.race?.trim()) {
      setActivePage('identity')
      setError('Character name and race are required before saving.')
      return
    }

    setSavingCharacter(true)
    setError('')
    try {
      await createCharacter({
        ...flattenCharacter(draftCharacter),
        campaign_id: campaign.id,
      })
      clearStoredDraft(campaign.id, currentUser?.id)
      const readyData = await setPlanningReady(campaign.id, true)
      setPlanning(readyData.planning)
      setFlowMode('waiting')
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingCharacter(false)
    }
  }

  const handleBondAction = async (bond, action) => {
    try {
      const edit = bondEdits[bond.id] || {}
      const payload = action === 'edit'
        ? { action, title: edit.title || bond.title, description: edit.description || bond.description }
        : { action }
      const data = await updatePlanningBond(campaign.id, bond.id, payload)
      setPlanning(data.planning)
      setBondEdits((prev) => ({ ...prev, [bond.id]: null }))
    } catch (err) {
      setError(err.message)
    }
  }

  if (loading) return <Loading />
  if (error && !planning) return <ErrorMessage message={error} />
  if (!planning) return <ErrorMessage message="Planning data could not be loaded." />

  const summary = planning.summary || {}
  const explicitPoints = summary.explicit_player_points || {}
  const ownSecrets = summary.dm_private_secrets?.[String(currentUser?.id)] || []
  const pendingBonds = (planning.bonds || []).filter((bond) => bond.status === 'pending')
  const blockingPendingBonds = pendingBondsForUser(pendingBonds, currentUser?.id)
  const confirmedBonds = (planning.bonds || []).filter((bond) => bond.status === 'confirmed')
  const effectiveFlowMode = flowMode || (selectedCharacter && isReady ? 'waiting' : 'choice')
  const currentPageIndex = Math.max(0, CHARACTER_FORM_PAGES.findIndex((page) => page.key === activePage))
  const currentPage = CHARACTER_FORM_PAGES[currentPageIndex] || CHARACTER_FORM_PAGES[0]

  const renderPartyReadiness = () => (
    <section className="planning-panel">
      <div className="planning-section-title">
        <i className="bi bi-people-fill"></i> Party Readiness
      </div>
      <div className="planning-member-list">
        {planning.members.map((member) => {
          const character = member.selected_character
          return (
            <div key={member.user_id} className={`planning-member ${member.is_character_ready ? 'ready' : ''}`}>
              <div className="planning-avatar" style={{ background: getGradientSeed(member.username || '') }}>
                {getInitials(member.username)}
              </div>
              <div className="planning-member-info">
                <strong>{member.username}{member.user_id === currentUser?.id ? ' (you)' : ''}</strong>
                <span>{character ? `${character.name} - ${getClassSummary(character)}` : 'Needs a character'}</span>
              </div>
              <div className="planning-ready-pill">
                {member.is_character_ready ? 'Ready' : 'Planning'}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )

  const renderPartyInfoPanel = ({ collapsible = false, includeReadiness = false } = {}) => {
    const isCollapsed = collapsible && partyInfoCollapsed

    return (
      <section className={`planning-panel planning-party-info ${isCollapsed ? 'collapsed' : ''}`}>
        <div className="planning-party-info-header">
          <div className="planning-section-title">
            <i className="bi bi-diagram-3-fill"></i> Party Info
          </div>
          {collapsible && (
            <button
              type="button"
              className="planning-collapse-btn"
              onClick={() => setPartyInfoCollapsed((value) => !value)}
              title={isCollapsed ? 'Expand party info' : 'Collapse party info'}
            >
              <i className={`bi ${isCollapsed ? 'bi-chevron-left' : 'bi-chevron-right'}`}></i>
            </button>
          )}
        </div>

        {!isCollapsed && (
          <div className="planning-party-info-body">
            {includeReadiness && (
              <div className="planning-party-readiness-inline">
                {planning.members.map((member) => {
                  const character = member.selected_character
                  return (
                    <div key={member.user_id} className={`planning-member ${member.is_character_ready ? 'ready' : ''}`}>
                      <div className="planning-avatar" style={{ background: getGradientSeed(member.username || '') }}>
                        {getInitials(member.username)}
                      </div>
                      <div className="planning-member-info">
                        <strong>{member.username}{member.user_id === currentUser?.id ? ' (you)' : ''}</strong>
                        <span>{character ? `${character.name} - ${getClassSummary(character)}` : 'Needs a character'}</span>
                      </div>
                      <div className="planning-ready-pill">
                        {member.is_character_ready ? 'Ready' : 'Planning'}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
            <p className="planning-summary-text">{summary.party_balance || 'The DM has not summarized the party yet.'}</p>
            <div className="planning-party-info-grid">
              <PlanningList title="Public Facts" items={listFrom(summary.confirmed_public_facts)} />
              <PlanningList title="Open Gaps" items={listFrom(summary.unresolved_gaps)} />
              <PlanningList title="Accepted Hooks" items={[...listFrom(summary.accepted_hooks), ...confirmedBonds.map((bond) => `${bond.title}: ${bond.description}`)]} />
              <PlanningList title="Your Private Notes" items={ownSecrets} />
              <PlanningList title="Your Explicit Points" items={explicitPoints[String(currentUser?.id)] || []} />
            </div>
          </div>
        )}
      </section>
    )
  }

  const renderPendingBondCards = () => pendingBonds.map((bond) => {
    const editing = bondEdits[bond.id]
    const currentApproval = bond.approval_states?.[String(currentUser?.id)]
    const acceptedByCurrentUser = currentApproval === 'accepted'
    const waitingForCount = (bond.involved_user_ids || []).filter((userId) => (
      String(userId) !== String(currentUser?.id)
        && bond.approval_states?.[String(userId)] !== 'accepted'
    )).length
    const waitingLabel = waitingForCount === 1 ? 'Waiting for the other player' : 'Waiting for other players'

    return (
      <div className="planning-chat-bond" key={`bond-${bond.id}`}>
        <div className="planning-chat-meta">
          <span>Bond Proposal</span>
          {acceptedByCurrentUser && <span className="planning-bond-status">Accepted by you</span>}
        </div>
        {editing ? (
          <>
            <input
              className="input"
              value={editing.title}
              onChange={(e) => setBondEdits((prev) => ({ ...prev, [bond.id]: { ...editing, title: e.target.value } }))}
            />
            <textarea
              className="textarea"
              value={editing.description}
              onChange={(e) => setBondEdits((prev) => ({ ...prev, [bond.id]: { ...editing, description: e.target.value } }))}
              rows={4}
            />
          </>
        ) : (
          <>
            <h4>{bond.title}</h4>
            <p>{bond.description}</p>
          </>
        )}
        {!editing && acceptedByCurrentUser && (
          <div className="planning-bond-waiting" role="status" aria-live="polite">
            <i className="bi bi-hourglass-split"></i>
            <span>{waitingLabel}</span>
          </div>
        )}
        <div className="planning-bond-actions">
          {editing ? (
            <>
              <button className="btn btn-primary small" onClick={() => handleBondAction(bond, 'edit')}>Save Edit</button>
              <button className="btn btn-secondary small" onClick={() => setBondEdits((prev) => ({ ...prev, [bond.id]: null }))}>Cancel</button>
            </>
          ) : (
            <>
              {!acceptedByCurrentUser && (
                <button className="btn btn-primary small" onClick={() => handleBondAction(bond, 'accept')}>Accept</button>
              )}
              <button
                className="btn btn-secondary small"
                onClick={() => setBondEdits((prev) => ({ ...prev, [bond.id]: { title: bond.title, description: bond.description } }))}
              >
                Edit
              </button>
              <button className="btn btn-danger small" onClick={() => handleBondAction(bond, 'decline')}>Decline</button>
            </>
          )}
        </div>
      </div>
    )
  })

  const renderPlanningContext = () => (
    <>
      {renderPartyInfoPanel()}
    </>
  )

  const renderChatPanel = () => (
    <section className="planning-panel planning-chat-panel planning-chat-panel-compact">
      <div className="planning-section-title">
        <i className="bi bi-stars"></i> DM Character Workshop
      </div>
      <div className="planning-chat-messages" ref={chatMessagesRef} onScroll={handleScroll}>
        {planning.messages.length === 0 && pendingBonds.length === 0 && (
          <div className="planning-empty-chat">
            Start with your concept, class ideas, tone, secrets, or what you want tied into the story.
          </div>
        )}
        {planning.messages.map((message) => (
          <div key={message.id} className={`planning-chat-message ${message.role === 'dm' ? 'dm' : 'player'}`}>
            <div className="planning-chat-meta">
              <span>{message.role === 'dm' ? 'DM' : 'You'}</span>
              <time>{formatTime(message.created_at)}</time>
            </div>
            <div className="planning-chat-body">
              {message.role === 'dm' ? <MarkdownContent content={message.content} /> : message.content}
            </div>
          </div>
        ))}
        {renderPendingBondCards()}
        {sending && (
          <div className="planning-chat-message dm">
            <div className="planning-chat-meta"><span>DM</span></div>
            <div className="planning-chat-body">
              {streamingContent ? (
                <MarkdownContent content={streamingContent} />
              ) : (
                <div className="planning-streaming-indicator">
                  <span className="planning-dot-pulse" />
                  <span>Thinking...</span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      <div className="planning-chat-input">
        <textarea
          className="textarea"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              handleSend()
            }
          }}
          placeholder="Talk with the DM about your character..."
          disabled={sending}
          rows={2}
        />
        <div className="planning-chat-actions">
          <button className="btn btn-primary" onClick={handleSend} disabled={sending || !input.trim()}>
            Send
          </button>
        </div>
      </div>
    </section>
  )

  const renderImportModal = () => showImport && (
    <div className="modal-overlay" onClick={() => setShowImport(false)}>
      <div className="modal-panel import-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Use Existing Character</h2>
          <button className="modal-close" onClick={() => setShowImport(false)}><i className="bi bi-x-lg"></i></button>
        </div>
        <div className="import-modal-body">
          {importLoading && availableChars.length === 0 ? (
            <div className="loading" style={{ padding: 24 }}>Loading characters...</div>
          ) : blockingPendingBonds.length > 0 ? (
            <div className="empty-state-v2" style={{ padding: 24 }}>
              <p>Resolve pending bond proposals before marking ready.</p>
            </div>
          ) : availableChars.length === 0 ? (
            <div className="empty-state-v2" style={{ padding: 24 }}>
              <p>No characters are available.</p>
            </div>
          ) : (
            <div className="import-char-list">
              {availableChars.map((character) => (
                <button
                  key={character.id}
                  className="import-char-row"
                  onClick={() => handleSelectCharacter(character.id)}
                  disabled={importLoading}
                >
                  <div className="import-char-avatar" style={{ background: getGradientSeed(character.name) }}>{getInitials(character.name)}</div>
                  <div className="import-char-info">
                    <div className="import-char-name">{character.name}</div>
                    <div className="import-char-meta">{character.race} - {getClassSummary(character)}</div>
                  </div>
                  <i className="bi bi-chevron-right import-char-arrow"></i>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )

  const renderChoice = () => (
    <div className="planning-choice-layout">
      <section className="planning-choice-panel">
        <div className="planning-choice-copy">
          <h2>Choose how you want to join</h2>
          <p>Pick an existing character and wait for the table, or build a new sheet with the AI DM beside the form.</p>
        </div>
        {selectedCharacter && !isReady && (
          <div className="planning-selected-character planning-selected-character-large">
            <div className="planning-character-avatar" style={{ background: getGradientSeed(selectedCharacter.name) }}>
              {getInitials(selectedCharacter.name)}
            </div>
            <div>
              <h3>{selectedCharacter.name}</h3>
              <p>{selectedCharacter.race} - {getClassSummary(selectedCharacter)}</p>
            </div>
          </div>
        )}
        <div className="planning-choice-actions">
          {selectedCharacter && !isReady && (
            <button className="planning-choice-card" onClick={handleReadySelectedCharacter} disabled={importLoading || blockingPendingBonds.length > 0}>
              <i className="bi bi-check2-circle"></i>
              <span>Use Selected Character</span>
              <small>Keep the assigned sheet and mark yourself ready.</small>
            </button>
          )}
          <button className="planning-choice-card" onClick={openImport}>
            <i className="bi bi-download"></i>
            <span>Use Existing Character</span>
            <small>Select one of your saved characters and mark ready.</small>
          </button>
          <button className="planning-choice-card" onClick={() => setFlowMode('create')}>
            <i className="bi bi-plus-lg"></i>
            <span>Create New Character</span>
            <small>Chat with the AI DM while filling out a new sheet.</small>
          </button>
        </div>
      </section>
      <div className="planning-choice-context">
        {renderPartyReadiness()}
        {renderPlanningContext()}
      </div>
    </div>
  )

  const renderWaiting = () => (
    <div className="planning-wait-layout">
      <section className="planning-panel planning-wait-panel">
        <div className="planning-wait-icon">
          <i className="bi bi-hourglass-split"></i>
        </div>
        <h2>{planning.all_ready ? 'The party is ready' : 'Waiting for the campaign to start'}</h2>
        {selectedCharacter ? (
          <div className="planning-selected-character planning-selected-character-large">
            <div className="planning-character-avatar" style={{ background: getGradientSeed(selectedCharacter.name) }}>
              {getInitials(selectedCharacter.name)}
            </div>
            <div>
              <h3>{selectedCharacter.name}</h3>
              <p>{selectedCharacter.race} - {getClassSummary(selectedCharacter)}</p>
            </div>
          </div>
        ) : (
          <p className="planning-muted">No character is selected.</p>
        )}
        <div className="planning-action-row planning-wait-actions">
          {planning.all_ready && (
            <button className="btn btn-primary" onClick={onComplete}>
              <i className="bi bi-door-open-fill"></i> Enter Dashboard
            </button>
          )}
          <button className="btn btn-secondary" onClick={handleChangeCharacter}>
            <i className="bi bi-arrow-left-right"></i> Change Character
          </button>
        </div>
      </section>
      <div className="planning-choice-context">
        {renderPartyReadiness()}
        {renderPlanningContext()}
      </div>
    </div>
  )

  const renderCreate = () => (
    <div className={`planning-create-layout ${partyInfoCollapsed ? 'party-collapsed' : ''}`}>
      <div className="planning-create-chat">
        {renderChatPanel()}
      </div>

      <form className="planning-panel character-form planning-draft-form" onSubmit={handleSaveCharacter}>
        <div className="planning-form-header">
          <div>
            <div className="planning-section-title">
              <i className="bi bi-person-lines-fill"></i> Character Sheet
            </div>
            <h2>{currentPage.label}</h2>
          </div>
          <div className="planning-page-counter">
            {currentPageIndex + 1} / {CHARACTER_FORM_PAGES.length}
          </div>
        </div>

        <div className="planning-page-tabs">
          {CHARACTER_FORM_PAGES.map((page) => (
            <button
              key={page.key}
              type="button"
              className={`planning-page-tab ${page.key === activePage ? 'active' : ''}`}
              onClick={() => setActivePage(page.key)}
            >
              <i className={`bi ${page.icon}`}></i>
              <span>{page.label}</span>
            </button>
          ))}
        </div>

        {pendingSuggestions.length > 0 && (
          <div className="planning-suggestions">
            <div className="planning-section-title">
              <i className="bi bi-lightbulb"></i> Suggested Overwrites
            </div>
            {pendingSuggestions.map((suggestion, index) => (
              <div className="planning-suggestion" key={`${suggestion.path}-${index}`}>
                <div>
                  <strong>{pathLabel(suggestion.path)}</strong>
                  <span>{String(Array.isArray(suggestion.value) ? `${suggestion.value.length} item(s)` : suggestion.value)}</span>
                </div>
                <button type="button" className="btn btn-secondary small" onClick={() => applySuggestion(suggestion)}>
                  Apply
                </button>
              </div>
            ))}
          </div>
        )}

        {blockingPendingBonds.length > 0 && (
          <div className="planning-suggestions">
            <div className="planning-section-title">
              <i className="bi bi-link-45deg"></i> Pending Bonds
            </div>
            <p className="planning-muted">Resolve pending bond proposals before marking ready.</p>
          </div>
        )}

        <CharacterFormBody
          character={draftCharacter}
          setCharacter={setDraftCharacter}
          sections={currentPage.sections}
          onFieldTouched={markFieldTouched}
        />

        <div className="planning-form-nav">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setActivePage(CHARACTER_FORM_PAGES[Math.max(0, currentPageIndex - 1)].key)}
            disabled={currentPageIndex === 0}
          >
            <i className="bi bi-chevron-left"></i> Previous
          </button>
          {currentPageIndex < CHARACTER_FORM_PAGES.length - 1 ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setActivePage(CHARACTER_FORM_PAGES[currentPageIndex + 1].key)}
            >
              Next <i className="bi bi-chevron-right"></i>
            </button>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={savingCharacter || blockingPendingBonds.length > 0}>
              {savingCharacter ? 'Saving...' : 'Save Character'}
            </button>
          )}
        </div>
      </form>
      <aside className="planning-create-party">
        {renderPartyInfoPanel({ collapsible: true, includeReadiness: true })}
      </aside>
    </div>
  )

  return (
    <div className={`planning-page ${effectiveFlowMode === 'create' ? 'planning-page-create' : ''}`}>
      {error && (
        <div className="planning-error" onClick={() => setError('')}>
          {error} <span>&times;</span>
        </div>
      )}

      <header className="planning-header">
        <button className="dashboard-back" onClick={() => navigate('/')} title="Back to campaigns">
          <i className="bi bi-arrow-left"></i>
        </button>
        <div>
          <h1>{campaign.name}</h1>
          <p>Character planning</p>
        </div>
        {planning.all_ready && effectiveFlowMode !== 'waiting' && (
          <button className="btn btn-primary" onClick={onComplete}>
            <i className="bi bi-door-open-fill"></i> Enter Dashboard
          </button>
        )}
      </header>

      <LlmPlayerManager
        campaignId={campaign.id}
        enabled={showLlmTools}
        isOwner={campaign.user_id === currentUser?.id}
        onAdded={onLlmPlayerAdded}
      />

      {effectiveFlowMode === 'create' ? renderCreate() : effectiveFlowMode === 'waiting' ? renderWaiting() : renderChoice()}
      {renderImportModal()}
    </div>
  )
}

function PlanningList({ title, items }) {
  return (
    <div className="planning-list-block">
      <h4>{title}</h4>
      {items.length === 0 ? (
        <p className="planning-muted">None yet.</p>
      ) : (
        <ul>
          {items.map((item, index) => <li key={`${title}-${index}`}>{item}</li>)}
        </ul>
      )}
    </div>
  )
}
